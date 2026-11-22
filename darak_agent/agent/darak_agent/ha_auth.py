"""Home Assistant authentication and service-user commands."""

import asyncio
import json
import secrets
import stat
import re
from pathlib import Path

import aiohttp

from .enrollment import _object, write_private_json
from .supervisor import MAX_RESPONSE_BYTES, TIMEOUT_SECONDS, UpstreamError

USERNAME = "darak-service"
DISPLAY_NAME = "Darak"
CLIENT_ID = "http://darak-agent/"
PASSWORD_PATH = "/data/darak-user.json"
MANAGED_USERS_PATH = "/data/managed-users.json"
MANAGED_USERNAME = re.compile(r"darak_[0-9a-f]{32}\Z")
SSO_APP_CLIENTS = {
    ("https://home-assistant.io/iOS", "homeassistant://auth-callback"),
    ("https://home-assistant.io/android", "homeassistant://auth-callback"),
}


class HaCommandError(UpstreamError):
    def __init__(self, code, message):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else "unknown"
        self.message = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", message)[:200] if isinstance(message, str) else ""
        super().__init__()


def _managed_user(users, user_id=None, username=None):
    if not isinstance(users, list):
        raise UpstreamError()
    matches = []
    for user in users:
        if not isinstance(user, dict) or not isinstance(user.get("credentials", []), list):
            raise UpstreamError()
        if user.get("username") is not None and not isinstance(user["username"], str):
            raise UpstreamError()
        if any(not isinstance(item, dict) or not isinstance(item.get("type"), str)
               or (item.get("username") is not None and not isinstance(item["username"], str))
               for item in user["credentials"]):
            raise UpstreamError()
        credentials = [item for item in user["credentials"] if item.get("type") == "homeassistant"]
        login = None
        if len(credentials) == 1:
            login = user.get("username")
            if not isinstance(login, str):
                login = credentials[0].get("username")
        if login and MANAGED_USERNAME.fullmatch(login) and (user_id is None or user.get("id") == user_id):
            if username is None or login == username:
                shaped = {key: user.get(key) for key in (
                    "id", "name", "is_active", "is_owner", "system_generated", "group_ids")}
                shaped["username"] = login
                shaped["credentials"] = user["credentials"]
                shaped["local_credential_attached"] = len(credentials) == 1
                matches.append(shaped)
    if len(matches) > 1:
        raise UpstreamError()
    return matches[0] if matches else None


def _managed_users(users):
    if not isinstance(users, list):
        raise UpstreamError()
    managed = []
    for user in users:
        match = _managed_user([user])
        if match is not None:
            managed.append(match)
    return managed


async def managed_user_list(client, params=None):
    users, = await client.admin_ws([{"type": "config/auth/list"}])
    return {"users": _managed_users(users)}


def _require_managed_target(user, username):
    if (not isinstance(username, str) or not MANAGED_USERNAME.fullmatch(username)
            or user is None or user.get("username") != username
            or user.get("is_owner") is not False or user.get("system_generated") is not False):
        from .commands import CommandError
        raise CommandError("conflict")
    credentials = user.get("credentials")
    if (not isinstance(credentials, list) or len(credentials) != 1
            or not isinstance(credentials[0], dict)
            or credentials[0].get("type") != "homeassistant"
            or (credentials[0].get("username") is not None
                and credentials[0]["username"] != username)):
        from .commands import CommandError
        raise CommandError("conflict")


async def _target(client, user_id, username=None):
    users, = await client.admin_ws([{"type": "config/auth/list"}])
    candidates = users if isinstance(users, list) else []
    if username is None:
        ids = [u for u in candidates if isinstance(u, dict) and u.get("id") == user_id]
        target = _managed_user(ids, user_id=user_id)
        username = target.get("username") if target else None
    target = _managed_user(candidates, user_id=user_id, username=username)
    _require_managed_target(target, username)
    return target


async def managed_user_create(client, params):
    from .commands import CommandError
    username, name, group = params["username"], params["name"], params["group"]
    if not MANAGED_USERNAME.fullmatch(username) or group not in {"system-admin", "system-users"}:
        raise CommandError("invalid_params")
    users, = await client.admin_ws([{"type": "config/auth/list"}])
    if _managed_user(users, username=username):
        raise CommandError("conflict")
    created, = await client.admin_ws([{"type": "config/auth/create", "name": name, "group_ids": [group]}])
    user = created.get("user") if isinstance(created, dict) else None
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, str):
        raise UpstreamError()
    password = secrets.token_urlsafe(48)
    try:
        await client.admin_ws([{"type": "config/auth_provider/homeassistant/create", "user_id": user_id,
                                "username": username, "password": password}])
        passwords = _managed_passwords(MANAGED_USERS_PATH)
        passwords[user_id] = password
        write_private_json(MANAGED_USERS_PATH, passwords)
    except Exception:
        await client.admin_ws([{"type": "config/auth/delete", "user_id": user_id}])
        raise
    return {"user_id": user_id}


async def managed_user_update(client, params):
    target = await _target(client, params["user_id"])
    message = {"type": "config/auth/update", "user_id": target["id"]}
    if "name" in params: message["name"] = params["name"]
    if "group" in params: message["group_ids"] = [params["group"]]
    if "is_active" in params: message["is_active"] = params["is_active"]
    await client.admin_ws([message])
    return {"updated": True}


async def managed_user_delete(client, params):
    target = await _target(client, params["user_id"])
    await client.admin_ws([{"type": "config/auth/delete", "user_id": target["id"]}])
    passwords = _managed_passwords(MANAGED_USERS_PATH)
    if passwords.pop(target["id"], None) is not None:
        write_private_json(MANAGED_USERS_PATH, passwords)
    return {"deleted": True}


async def managed_user_sso_code(client, params):
    from .commands import CommandError
    username = params["username"]
    if not MANAGED_USERNAME.fullmatch(username):
        raise CommandError("invalid_params")
    client_id, redirect_uri = params["client_id"], params["redirect_uri"]
    web = re.fullmatch(r"https://([a-z][a-z0-9-]{0,30}[a-z0-9]\.darak\.ps)/", client_id or "")
    if web:
        expected = f"https://{web.group(1)}/?auth_callback=1"
        if redirect_uri != expected:
            raise CommandError("invalid_params")
    elif (client_id, redirect_uri) not in SSO_APP_CLIENTS:
        raise CommandError("invalid_params")
    user = await _target(client, params["user_id"], username)
    password = _managed_passwords(MANAGED_USERS_PATH).get(user["id"])
    if not isinstance(password, str):
        raise CommandError("conflict")
    try:
        status, body = await client.ha_http("POST", "/auth/login_flow", json={
            "client_id": client_id, "handler": ["homeassistant", None], "redirect_uri": redirect_uri})
        flow = json.loads(body)
        if (status != 200 or not isinstance(flow, dict) or not isinstance(flow.get("flow_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", flow["flow_id"])):
            return {"reason": "login_flow_start_failed"}
        if flow.get("type") != "form" or flow.get("step_id") != "init":
            return {"reason": "unexpected_first_form"}
        status, body = await client.ha_http("POST", "/auth/login_flow/" + flow["flow_id"], json={
            "client_id": client_id, "username": username, "password": password})
        entry = json.loads(body)
        if status != 200 or not isinstance(entry, dict):
            return {"reason": "login_flow_submit_failed"}
        errors = entry.get("errors")
        base_error = errors.get("base") if isinstance(errors, dict) else None
        return {
            "type": entry.get("type"), "step_id": entry.get("step_id"),
            "base_error": base_error if isinstance(base_error, str) and len(base_error) <= 64 else None,
            "result": entry.get("result") if isinstance(entry.get("result"), str) else None,
        }
    except Exception:
        return {"reason": "login_flow_submit_failed"}


def _managed_passwords(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise UpstreamError() from None
    try:
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        passwords = json.loads(path.read_text(), object_pairs_hook=_object)
        if not isinstance(passwords, dict) or any(
                not isinstance(user_id, str) or not isinstance(password, str) or not password
                for user_id, password in passwords.items()):
            raise ValueError
        return passwords
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise UpstreamError() from None


def managed_user_is_stored(user_id):
    return user_id in _managed_passwords(MANAGED_USERS_PATH)


async def _ws(url, token, messages):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as session:
            async with session.ws_connect(url, max_msg_size=MAX_RESPONSE_BYTES) as ws:
                hello = await ws.receive_json()
                if not isinstance(hello, dict) or hello.get("type") != "auth_required":
                    raise UpstreamError()
                await ws.send_json({"type": "auth", "access_token": token})
                auth = await ws.receive_json()
                if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
                    raise UpstreamError()
                results = []
                for ident, message in enumerate(messages, 1):
                    await ws.send_json({"id": ident, **message})
                    response = await ws.receive_json()
                    if (not isinstance(response, dict) or response.get("id") != ident
                            or response.get("type") != "result"):
                        raise UpstreamError()
                    if response.get("success") is False:
                        error = response.get("error")
                        error = error if isinstance(error, dict) else {}
                        raise HaCommandError(error.get("code"), error.get("message"))
                    if response.get("success") is not True:
                        raise UpstreamError()
                    result = response.get("result")
                    if len(json.dumps(result, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
                        raise UpstreamError()
                    results.append(result)
                return results
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, RecursionError):
        raise UpstreamError() from None


async def admin_ws(messages, *, supervisor_url, supervisor_token):
    url = supervisor_url.replace("http", "ws", 1).rstrip("/") + "/core/websocket"
    return await _ws(url, supervisor_token, messages)


async def user_ws(access_token, messages, *, ha_upstream_host, ha_upstream_port):
    return await _ws(f"ws://{ha_upstream_host}:{ha_upstream_port}/api/websocket", access_token, messages)


async def http(method, path, *, json=None, form=None, token=None, ha_upstream_host, ha_upstream_port):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as session:
            async with session.request(method, f"http://{ha_upstream_host}:{ha_upstream_port}{path}",
                                       json=json, data=form, allow_redirects=False,
                                       headers={"Authorization": "Bearer " + token} if token else None) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise UpstreamError()
                    body.extend(chunk)
                return response.status, bytes(body)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise UpstreamError() from None


def _load_password(path, *, create=False):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise _conflict() from None
        password = secrets.token_urlsafe(32)
        write_private_json(path, {"password": password})
        return password
    try:
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        value = json.loads(path.read_text(), object_pairs_hook=_object)
        if not isinstance(value, dict) or value.keys() != {"password"} or not isinstance(value["password"], str):
            raise ValueError
        return value["password"]
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise _conflict() from None


def _conflict():
    from .commands import CommandError
    return CommandError("conflict")


def _find_user(users):
    if not isinstance(users, list):
        raise UpstreamError()
    for user in users:
        if not isinstance(user, dict) or not isinstance(user.get("credentials", []), list):
            continue
        for credential in user.get("credentials", []):
            if (isinstance(credential, dict) and credential.get("type") == "homeassistant"
                    and (user.get("username") or credential.get("username")) == USERNAME):
                return user
    return None


def _orphaned_user(users):
    matches = [user for user in users if isinstance(user, dict)
               and user.get("system_generated") is False and user.get("name") == DISPLAY_NAME
               and user.get("credentials") == []]
    if len(matches) > 1:
        raise UpstreamError()
    return matches[0] if matches else None


async def service_user_ensure(client, params=None):
    password = _load_password(client.password_path, create=True)
    listed, = await client.admin_ws([{"type": "config/auth/list"}])
    user = _find_user(listed)
    if user is None:
        orphan = _orphaned_user(listed)
        if orphan is not None:
            user_id = orphan.get("id")
            if not isinstance(user_id, str):
                raise UpstreamError()
            await client.admin_ws([{"type": "config/auth_provider/homeassistant/create", "user_id": user_id,
                                    "username": USERNAME, "password": password}])
            await client.admin_ws([{"type": "config/auth/update", "user_id": user_id, "name": DISPLAY_NAME,
                                    "is_active": True, "group_ids": ["system-admin"]}])
            return {"user_id": user_id, "username": USERNAME, "created": False}
        response, = await client.admin_ws([{"type": "config/auth/create", "name": DISPLAY_NAME,
                                           "group_ids": ["system-admin"], "local_only": False}])
        created_user = response.get("user") if isinstance(response, dict) else None
        user_id = created_user.get("id") if isinstance(created_user, dict) else None
        if not isinstance(user_id, str):
            raise UpstreamError()
        await client.admin_ws([{"type": "config/auth_provider/homeassistant/create", "user_id": user_id,
                                "username": USERNAME, "password": password}])
        return {"user_id": user_id, "username": USERNAME, "created": True}
    user_id = user.get("id")
    if not isinstance(user_id, str):
        raise UpstreamError()
    await client.admin_ws([{"type": "config/auth/update", "user_id": user_id, "name": DISPLAY_NAME,
                            "is_active": True, "group_ids": ["system-admin"]}])
    await client.admin_ws([{"type": "config/auth_provider/homeassistant/delete", "username": USERNAME}])
    await client.admin_ws([{"type": "config/auth_provider/homeassistant/create", "user_id": user_id,
                            "username": USERNAME, "password": password}])
    return {"user_id": user_id, "username": USERNAME, "created": False}


async def _login(client):
    password = _load_password(client.password_path)
    status, body = await client.ha_http("POST", "/auth/login_flow", json={
        "client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID})
    flow = _json_response(status, body)
    if not isinstance(flow.get("flow_id"), str):
        raise UpstreamError()
    status, body = await client.ha_http("POST", "/auth/login_flow/" + flow["flow_id"], json={
        "client_id": CLIENT_ID, "username": USERNAME, "password": password})
    entry = _json_response(status, body)
    if entry.get("type") != "create_entry" or not isinstance(entry.get("result"), str):
        raise UpstreamError()
    code = entry["result"]
    status, body = await client.ha_http("POST", "/auth/token", form={
        "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID})
    tokens = _json_response(status, body)
    if not all(isinstance(tokens.get(key), str) for key in ("access_token", "refresh_token")):
        raise UpstreamError()
    return tokens["access_token"], tokens["refresh_token"]


def _json_response(status, body):
    try:
        result = json.loads(body)
        if status != 200 or not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, RecursionError):
        raise UpstreamError() from None


async def _session(client, purpose, issue):
    access_token, refresh_token = await _login(client)
    client_name = "darak-" + purpose
    try:
        tokens, = await client.user_ws(access_token, [{"type": "auth/refresh_tokens"}])
        matches = _matching_tokens(tokens, client_name)
        if matches and issue:
            raise _conflict()
        if issue:
            created, = await client.user_ws(access_token, [{"type": "auth/long_lived_access_token",
                                                           "client_name": client_name, "lifespan": 3650}])
            token = created
            if not isinstance(token, str):
                raise UpstreamError()
            tokens, = await client.user_ws(access_token, [{"type": "auth/refresh_tokens"}])
            matches = _matching_tokens(tokens, client_name)
            if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
                raise UpstreamError()
            match = matches[0]
            return {"token": token, "refresh_token_id": match["id"], "client_name": client_name}
        if matches:
            await client.user_ws(access_token, [
                {"type": "auth/delete_refresh_token", "refresh_token_id": item["id"]}
                for item in matches
            ])
        return {"revoked": len(matches)}
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh_token})
        except Exception:
            pass


def _matching_tokens(tokens, client_name):
    if not isinstance(tokens, list) or any(not isinstance(item, dict) for item in tokens):
        raise UpstreamError()
    return [item for item in tokens if item.get("type") == "long_lived_access_token"
            and item.get("client_name") == client_name]


async def token_issue(client, params):
    return await _session(client, params["purpose"], True)


async def token_revoke(client, params):
    return await _session(client, params["purpose"], False)


async def service_user_remove(client, params=None):
    listed, = await client.admin_ws([{"type": "config/auth/list"}])
    user = _find_user(listed)
    if user is not None:
        await client.admin_ws([{"type": "config/auth/delete", "user_id": user["id"]}])
    try:
        Path(client.password_path).unlink()
    except FileNotFoundError:
        pass
    return {"removed": user is not None}


TECH_USERNAME = "darak-tech"
TECH_DISPLAY_NAME = "Darak Technician"


def _tech_users(users, user_id=None):
    """Every technician user: the recorded one, any with the technician login
    (for example one brought back by a restore) and half-created leftovers."""
    if not isinstance(users, list):
        raise UpstreamError()
    matches = []
    for user in users:
        if (not isinstance(user, dict) or not isinstance(user.get("credentials", []), list)
                or user.get("is_owner")):
            continue
        if (user_id is not None and user.get("id") == user_id) or user.get("username") == TECH_USERNAME:
            matches.append(user)
        elif any(isinstance(credential, dict) and credential.get("type") == "homeassistant"
                 and credential.get("username") == TECH_USERNAME for credential in user.get("credentials", [])):
            matches.append(user)
        elif (user.get("system_generated") is False and user.get("name") == TECH_DISPLAY_NAME
              and user.get("credentials") == []):
            matches.append(user)
    return matches


async def tech_user_disable(client, params=None):
    try:
        state = json.loads(client.tech_path.read_text())
        user_id = state.get("user_id")
    except (FileNotFoundError, OSError, ValueError, TypeError):
        user_id = None
    listed, = await client.admin_ws([{"type": "config/auth/list"}])
    users = _tech_users(listed, user_id)
    for user in users:
        await client.admin_ws([{"type": "config/auth/delete", "user_id": user["id"]}])
    client.tech_path.unlink(missing_ok=True)
    return {"removed": bool(users)}


async def tech_user_enable(client, params):
    from datetime import datetime, timedelta, timezone
    from .commands import CommandError

    password, expires_at = params.get("password"), params.get("expires_at")
    if (not isinstance(password, str) or not 24 <= len(password) <= 128
            or any(not 32 <= ord(char) <= 126 for char in password)):
        raise CommandError("invalid_params")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        now = (getattr(client, "session_clock", None) or (lambda: datetime.now(timezone.utc)))()
        if (not expires_at.endswith("Z") or expiry.tzinfo != timezone.utc or expiry <= now
                or expiry > now + timedelta(hours=4, seconds=60)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CommandError("invalid_params") from None
    listed, = await client.admin_ws([{"type": "config/auth/list"}])
    try:
        old_id = json.loads(client.tech_path.read_text()).get("user_id")
    except (FileNotFoundError, ValueError, OSError):
        old_id = None
    old_users = _tech_users(listed, old_id)
    for old in old_users:
        await client.admin_ws([{"type": "config/auth/delete", "user_id": old["id"]}])
    response, = await client.admin_ws([{"type": "config/auth/create", "name": TECH_DISPLAY_NAME,
                                        # Keep False: remote-access sign-in shows HA the real public IP, so it refuses local_only; expiry sweep + shutdown revoke bound access.
                                        "group_ids": ["system-admin"], "local_only": False}])
    user = response.get("user") if isinstance(response, dict) else None
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, str):
        raise UpstreamError()
    try:
        await client.admin_ws([{"type": "config/auth_provider/homeassistant/create", "user_id": user_id,
                                "username": TECH_USERNAME, "password": password}])
    except Exception:
        await client.admin_ws([{"type": "config/auth/delete", "user_id": user_id}])
        raise
    write_private_json(client.tech_path, {"user_id": user_id, "expires_at": expires_at})
    return {"user_id": user_id, "username": TECH_USERNAME, "expires_at": expires_at}
