"""Bounded, replaceable Supervisor/Core access."""

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .redact import redact

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_STATES_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 15
SLUG_RE = re.compile(r"[a-z0-9_]{1,64}")
# Add-on logs the cloud may read: this agent (local or repository install) and the SSH add-ons the support flow uses.
LOG_ADDON_RE = re.compile(r"self|(?:local|[0-9a-f]{8})_darak_agent|core_ssh|a0d7b954_ssh")
JSON_PATHS = frozenset({
    "/info", "/host/info", "/os/info", "/core/info", "/supervisor/info",
    "/core/stats", "/supervisor/stats", "/resolution/info", "/addons", "/backups",
    "/core/api/config", "/core/api/states", "/addons/self/info",
})
POST_JSON_PATHS = frozenset({"/core/check", "/core/restart", "/host/reboot", "/core/update",
                             "/supervisor/update", "/os/update"})
POST_JSON_PATTERNS = (re.compile(r"/backups/new/partial"),
                      re.compile(r"/addons/[a-z0-9_]{1,64}/update"),
                      re.compile(r"/addons/(?:core_ssh|a0d7b954_ssh)/(?:options|restart)"),
                      re.compile(r"/backups/[0-9a-f]{8}/restore/partial"))
GET_JSON_PATTERNS = (re.compile(r"/addons/(?:core_ssh|a0d7b954_ssh)/info"),
                     re.compile(r"/jobs/[0-9a-f]{32}"),
                     re.compile(r"/backups/[0-9a-f]{8}/info"))
BACKUP_SLUG_RE = re.compile(r"[0-9a-f]{8}")
DELETE_PATHS = re.compile(r"/backups/[0-9a-f]{8}")
# A configuration check validates every integration; slow boards need longer.
CORE_CHECK_TIMEOUT_SECONDS = 120
# Supervisor answers an update call only once the update has finished.
UPDATE_TIMEOUT_SECONDS = 30 * 60
LOG_PATHS = {"core": "/core/logs", "supervisor": "/supervisor/logs", "host": "/host/logs"}
Transport = Callable[[str, str, dict], Awaitable[tuple[int, Mapping, bytes]]]
WSCall = Callable[[dict], Awaitable[object]]


class UpstreamError(Exception):
    def __init__(self):
        super().__init__("Invalid upstream response")


class BackupResponse:
    def __init__(self, session, response):
        self.session, self.response = session, response
        self.content = response.content

    async def release(self):
        self.response.release()
        await self.session.close()


def _post_timeout(path):
    if path.endswith("/update"):
        return UPDATE_TIMEOUT_SECONDS
    return CORE_CHECK_TIMEOUT_SECONDS if path == "/core/check" else TIMEOUT_SECONDS


def _response_limit(path):
    return MAX_STATES_BYTES if path == "/core/api/states" else MAX_RESPONSE_BYTES


async def default_transport(method, url, headers, body=None):
    path = urlsplit(url).path
    max_bytes = _response_limit(path)
    total = _post_timeout(path)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total)) as session:
        request = {"headers": headers, "allow_redirects": False}
        if body is not None:
            request["data"] = body
        async with session.request(method, url, **request) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                if len(body) + len(chunk) > max_bytes:
                    raise UpstreamError()
                body.extend(chunk)
            return response.status, response.headers, bytes(body)


async def clear_enrollment_option(supervisor_url, token, transport=default_transport):
    """Clear the one-time enrollment bundle from Supervisor options."""
    headers = {"Authorization": "Bearer " + token}
    async with asyncio.timeout(TIMEOUT_SECONDS):
        status, _, body = await transport("GET", supervisor_url.rstrip("/") + "/addons/self/info",
                                          headers)
    try:
        payload = json.loads(body)
        options = payload["data"]["options"]
        if status != 200 or not isinstance(options, dict):
            raise ValueError
        clean = {key: value for key, value in options.items() if key != "enrollment_bundle"}
        async with asyncio.timeout(TIMEOUT_SECONDS):
            status, _, _ = await transport(
                "POST", supervisor_url.rstrip("/") + "/addons/self/options",
                {**headers, "Content-Type": "application/json"},
                json.dumps({"options": clean}).encode(),
            )
        if status not in {200, 201}:
            raise UpstreamError()
    except (KeyError, TypeError, ValueError, RecursionError):
        raise UpstreamError() from None


def read_temperature(paths=None):
    values = []
    if paths is None:
        paths = Path("/sys/class/thermal").glob("thermal_zone*/temp")
    for path in paths:
        try:
            value = float(path.read_text()) / 1000
            if math.isfinite(value) and -50 <= value <= 200:
                values.append(value)
        except (OSError, ValueError):
            continue
    return max(values) if values else None


class SupervisorClient:
    def __init__(self, base_url: str, token: str, transport: Transport = default_transport,
                 *, ws_call: WSCall | None = None, admin_ws=None, user_ws=None, http=None,
                 ha_upstream_host="homeassistant", ha_upstream_port=8123,
                 password_path="/data/darak-user.json", config_dir="/homeassistant",
                 integrations_dir="/opt/darak-agent/integrations", voice_forward_port=10300,
                 voice_http=None,
                 backup_dir="/data/backups",
                 temperature_reader=read_temperature, tech_path="/data/tech.json", ssh_path="/data/ssh.json",
                 session_clock=None, http_original_path="/data/http-original.json"):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._ws_call = ws_call or self._default_ws_call
        self._admin_ws_call = admin_ws
        self._user_ws_call = user_ws
        self._http_call = http
        self.ha_upstream_host = ha_upstream_host
        self.ha_upstream_port = ha_upstream_port
        self.password_path = password_path
        self.config_dir = Path(config_dir)
        self.integrations_dir = Path(integrations_dir)
        self.voice_forward_port = voice_forward_port
        self.voice_http = voice_http
        self.backup_dir = Path(backup_dir)
        self.tech_path, self.ssh_path = Path(tech_path), Path(ssh_path)
        self.http_original_path = Path(http_original_path)
        self.session_clock = session_clock
        self._temperature_reader = temperature_reader

    def safe_text(self, value):
        if not isinstance(value, str):
            raise UpstreamError()
        return redact(value, secret=self._token)[:512]

    def temperature(self):
        try:
            value = self._temperature_reader()
            if type(value) in {int, float} and math.isfinite(value) and -50 <= value <= 200:
                return value
        except Exception:
            # Temperature is optional, including when the hardware reader fails.
            pass
        return None

    async def _get(self, path, extra_headers=None):
        headers = {"Authorization": "Bearer " + self._token}
        headers.update(extra_headers or {})
        async with asyncio.timeout(TIMEOUT_SECONDS):
            status, _, body = await self._transport("GET", self.base_url + path, headers)
        if not isinstance(body, bytes) or len(body) > _response_limit(path) or status not in {200, 206}:
            raise UpstreamError()
        return body

    async def get_json(self, path):
        if path not in JSON_PATHS and not any(pattern.fullmatch(path) for pattern in GET_JSON_PATTERNS):
            raise UpstreamError()
        body = await self._get(path)
        try:
            payload = json.loads(body)
            if path.startswith("/core/api/"):
                expected = list if path.endswith("/states") else dict
                if not isinstance(payload, expected):
                    raise ValueError
                return payload
            if (not isinstance(payload, dict) or payload.get("result") != "ok"
                    or not isinstance(payload.get("data"), dict)):
                raise ValueError
            return payload["data"]
        except (ValueError, RecursionError):
            raise UpstreamError() from None

    async def post_json(self, path, body=None):
        if path not in POST_JSON_PATHS and not any(pattern.fullmatch(path) for pattern in POST_JSON_PATTERNS):
            raise UpstreamError()
        try:
            encoded = None if body is None else json.dumps(body, allow_nan=False).encode()
            limit = _post_timeout(path)
            async with asyncio.timeout(limit):
                status, _, raw = await self._transport(
                    "POST", self.base_url + path,
                    {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}, encoded)
        except (aiohttp.ClientError, OSError, TimeoutError):
            # These restart the very service answering (or the whole host), so a
            # dropped connection is the expected outcome; the hub confirms by
            # watching versions and health come back.
            if path in {"/core/restart", "/host/reboot", "/supervisor/update", "/os/update"}:
                return {}
            raise
        try:
            if status != 200 or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError
            payload = json.loads(raw)
            data = payload.get("data", {}) if isinstance(payload, dict) else None
            if payload.get("result") != "ok" or not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise UpstreamError() from None

    async def request_json(self, method, path, body=None):
        if method != "POST" or not any(pattern.fullmatch(path) for pattern in POST_JSON_PATTERNS):
            raise UpstreamError()
        return await self.post_json(path, body)

    async def backup_upload(self, file_path):
        with Path(file_path).open("rb") as stream:
            form = aiohttp.FormData()
            form.add_field("file", stream, filename="backup.tar", content_type="application/x-tar")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30 * 60)) as session:
                async with session.post(self.base_url + "/backups/new/upload", data=form,
                                        headers={"Authorization": "Bearer " + self._token},
                                        allow_redirects=False) as response:
                    raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                    if response.status != 200 or len(raw) > MAX_RESPONSE_BYTES:
                        raise UpstreamError()
        try:
            payload = json.loads(raw)
            data = payload["data"]
            slug = data["slug"]
            if payload.get("result") != "ok" or not isinstance(slug, str) or not BACKUP_SLUG_RE.fullmatch(slug):
                raise ValueError
            return slug
        except (ValueError, KeyError, TypeError, RecursionError):
            raise UpstreamError() from None

    async def delete_json(self, path):
        if not DELETE_PATHS.fullmatch(path):
            raise UpstreamError()
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                status, _, raw = await self._transport("DELETE", self.base_url + path,
                                                       {"Authorization": "Bearer " + self._token})
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError
            payload = json.loads(raw)
            if status != 200 or payload.get("result") != "ok":
                raise ValueError
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise UpstreamError() from None

    async def backup_download(self, slug):
        if not isinstance(slug, str) or not BACKUP_SLUG_RE.fullmatch(slug):
            raise UpstreamError()
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=60))
        try:
            response = await session.get(self.base_url + f"/backups/{slug}/download",
                                         headers={"Authorization": "Bearer " + self._token},
                                         allow_redirects=False)
            if response.status != 200:
                response.release()
                await session.close()
                raise UpstreamError()
            return BackupResponse(session, response)
        except Exception:
            if not session.closed:
                await session.close()
            raise

    async def logs(self, source, addon, lines):
        if type(lines) is not int or not 1 <= lines <= 2000:
            raise UpstreamError()
        if source == "addon" and isinstance(addon, str) and LOG_ADDON_RE.fullmatch(addon):
            path = f"/addons/{addon}/logs"
        elif source in LOG_PATHS and addon is None:
            path = LOG_PATHS[source]
        else:
            raise UpstreamError()
        body = await self._get(path, {"Accept": "text/plain", "Range": f"entries=:-{lines}:"})
        # Error envelopes sometimes arrive with HTTP 200; never return them as logs.
        if body.lstrip().startswith(b"{"):
            try:
                envelope = json.loads(body)
            except (ValueError, RecursionError):
                envelope = None
            if isinstance(envelope, dict) and "result" in envelope:
                raise UpstreamError()
        return redact(body.decode("utf-8", errors="replace"), secret=self._token)

    async def integrations(self):
        async with asyncio.timeout(TIMEOUT_SECONDS):
            result = await self._ws_call({"type": "config_entries/get"})
        try:
            size = len(json.dumps(result, allow_nan=False).encode())
        except (ValueError, TypeError, RecursionError):
            raise UpstreamError() from None
        if not isinstance(result, list) or size > MAX_RESPONSE_BYTES:
            raise UpstreamError()
        return result

    async def _default_ws_call(self, message):
        return (await self._default_ws_messages([message]))[0]

    async def _default_ws_messages(self, messages, token=None, url=None):
        from .ha_auth import _ws
        url = url or self.base_url.replace("http", "ws", 1) + "/core/websocket"
        return await _ws(url, self._token if token is None else token, messages)

    async def admin_ws(self, messages):
        if self._admin_ws_call:
            return await self._admin_ws_call(messages)
        return await self._default_ws_messages(messages)

    async def user_ws(self, access_token, messages):
        if self._user_ws_call:
            return await self._user_ws_call(access_token, messages)
        return await self._default_ws_messages(
            messages, access_token, f"ws://{self.ha_upstream_host}:{self.ha_upstream_port}/api/websocket")

    async def ha_http(self, method, path, *, json=None, form=None, token=None):
        if self._http_call:
            return await self._http_call(method, path, json=json, form=form, **({"token": token} if token is not None else {}))
        from .ha_auth import http
        return await http(method, path, ha_upstream_host=self.ha_upstream_host,
                          ha_upstream_port=self.ha_upstream_port, json=json, form=form, token=token)
