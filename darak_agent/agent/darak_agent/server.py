"""Authenticated commands with replay persistence and fail-fast concurrency limits."""

import json
import hashlib
import logging
import math
import os
import re
import shutil
import stat
import time
from uuid import UUID

from aiohttp import web

from .commands import COMMANDS, CommandError
from .enrollment import write_private_json
from .metrics import render_metrics
from .signing import MAX_SKEW_SECONDS, request_string, response_string, sign, verify
from .supervisor import SupervisorClient

# A 256 KiB UTF-8 file can expand to about 1.5 MiB when JSON-escaped.
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REPLAY_ENTRIES = 4096
LOGGER = logging.getLogger("darak_agent.requests")
MESSAGES = {
    "unauthorized": "Unauthorized", "invalid_request": "Invalid request",
    "unknown_command": "Unknown command", "too_large": "Request too large",
    "busy": "Agent busy", "internal_error": "Internal error",
    "stale_request": "Request timestamp outside allowed window",
    "replayed": "Operation already used",
    "upstream_error": "Invalid upstream response",
    "ha_error": "Home Assistant command failed",
    "conflict": "Command not applicable in the current state",
}


def _load_replays(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {}
    try:
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict) or len(value) > MAX_REPLAY_ENTRIES:
            raise ValueError
        for operation, received in value.items():
            valid_time = (type(received) is int and 0 <= received <= 999_999_999_999
                          or type(received) is float and math.isfinite(received)
                          and 0 <= received <= 999_999_999_999)
            if (not isinstance(operation, str) or str(UUID(operation)) != operation or not valid_time):
                raise ValueError
        return value
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("Invalid agent replay cache") from None


def _operation(request):
    values = request.headers.getall("X-Darak-Operation", [])
    if len(values) == 1:
        try:
            if str(UUID(values[0])) == values[0]:
                return values[0]
        except ValueError:
            pass
    return None


def _error(status, code, operation=None, message=None):
    return web.json_response({"ok": False, "operation": operation,
                              "error": {"code": code, "message": message or MESSAGES[code]}},
                             status=status)


def create_app(config, *, client=None, clock=None, wall_clock=None, replay_path=None):
    client = client if client is not None else SupervisorClient(
        config.supervisor_url, config.supervisor_token, ha_upstream_host=config.ha_upstream_host,
        ha_upstream_port=config.ha_upstream_port, voice_forward_port=config.voice_forward_port)
    clock = clock or time.monotonic
    wall_clock = wall_clock or time.time
    active = 0
    last_health = None
    key = bytes.fromhex(config.signing_key) if config.signing_key else bytes(32)
    seen = _load_replays(replay_path) if replay_path is not None else {}
    download_active = False
    upload_active = False

    async def authenticate(request, handler, operation):
        timestamps = request.headers.getall("X-Darak-Timestamp", [])
        timestamp = timestamps[0] if len(timestamps) == 1 else ""
        valid_timestamp = re.fullmatch(r"[0-9]{1,12}", timestamp) is not None
        signatures = request.headers.getall("X-Darak-Signature", [])
        signature = signatures[0] if len(signatures) == 1 else ""
        # Do not short-circuit verification, even for absent or malformed headers.
        matches = verify(key, request_string(config.home_id, request.method, request.raw_path,
                                            timestamp if valid_timestamp else "",
                                            operation or "", request["body"]), signature)
        if not (matches and config.signing_key and operation and valid_timestamp):
            return _error(401, "unauthorized", operation)
        now = wall_clock()
        if abs(now - int(timestamp)) > MAX_SKEW_SECONDS:
            return _error(401, "stale_request", operation)
        # Scans at most 4096 entries; use an expiry queue if this cap grows.
        for previous, received in list(seen.items()):
            if now - received > 2 * MAX_SKEW_SECONDS:
                del seen[previous]
        if operation in seen:
            return _error(409, "replayed", operation)
        if len(seen) >= MAX_REPLAY_ENTRIES:
            return _error(429, "busy", operation)
        # No await between checking and recording: concurrent replays cannot pass.
        seen[operation] = now
        if replay_path is not None:
            try:
                write_private_json(replay_path, seen)
            except OSError:
                seen.pop(operation, None)
                raise
        return await handler(request)

    @web.middleware
    async def boundary(request, handler):
        started = clock()
        operation = None
        command = None
        status = 500
        try:
            try:
                try:
                    if request.path == "/v1/backups/upload" and request.method == "POST":
                        request["body"] = b""
                    else:
                        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
                            raise web.HTTPRequestEntityTooLarge(MAX_BODY_BYTES, request.content_length)
                        body = bytearray()
                        async for chunk in request.content.iter_chunked(4096):
                            if len(body) + len(chunk) > MAX_BODY_BYTES:
                                raise web.HTTPRequestEntityTooLarge(MAX_BODY_BYTES, len(body) + len(chunk))
                            body.extend(chunk)
                        request["body"] = bytes(body)
                finally:
                    operation = _operation(request)
                    name = request.match_info.get("name")
                    command = name if name in COMMANDS else None
                response = await authenticate(request, handler, operation)
            except web.HTTPRequestEntityTooLarge:
                response = _error(413, "too_large", operation)
            except web.HTTPException as exc:
                response = _error(exc.status, "invalid_request", operation)
            except Exception:
                # Never log upstream exception text: it may include credentials.
                response = _error(500, "internal_error", operation)
            if response.status == 401:
                command = None
            if operation:
                response.headers["X-Darak-Operation"] = operation
            # aiohttp suppresses HEAD bodies on the wire, including error bodies.
            if request.method == "HEAD":
                response.body = b""
            if not isinstance(response, web.StreamResponse) or isinstance(response, web.Response):
                response.headers["X-Darak-Signature"] = sign(
                    key, response_string(config.home_id, operation, response.status, response.body))
            status = response.status
            return response
        finally:
            LOGGER.info(json.dumps({"operation": operation, "command": command,
                                    "status": status, "duration_ms": round((clock() - started) * 1000, 3)}))

    async def command_handler(request):
        nonlocal active, last_health
        operation = _operation(request)
        name = request.match_info["name"]
        if name not in COMMANDS:
            return _error(404, "unknown_command", operation)
        # No await between checking and reserving: extra work never queues.
        if active >= 4:
            return _error(429, "busy", operation)
        active += 1
        try:
            body = request["body"]
            if not body:
                params = {}
            else:
                try:
                    payload = json.loads(body)
                except (ValueError, RecursionError):
                    return _error(400, "invalid_request", operation)
                if not isinstance(payload, dict) or set(payload) != {"params"}:
                    return _error(400, "invalid_request", operation)
                params = payload["params"]
            try:
                result = await COMMANDS[name].run(client, params)
            except CommandError as exc:
                status = (400 if exc.code == "invalid_params" else 403 if exc.code == "customer_blocked"
                          else 409 if exc.code in {"conflict", "approval_required"}
                          else 429 if exc.code == "busy" else 502)
                return _error(status,
                              exc.code, operation, exc.message)
            if name == "health":
                last_health = result
            return web.json_response({"ok": True, "operation": operation, "result": result})
        finally:
            active -= 1

    async def metrics_handler(request):
        return web.Response(text=render_metrics(last_health),
                            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})

    async def backup_stream_handler(request):
        nonlocal download_active
        operation = _operation(request)
        slug = request.match_info["slug"]
        if request.get("body", b"") or not re.fullmatch(r"[0-9a-f]{8}", slug):
            return _error(400, "invalid_request", operation)
        if download_active:
            return _error(429, "busy", operation)
        download_active = True
        try:
            try:
                upstream = await client.backup_download(slug)
            except Exception:
                return _error(502, "upstream_error", operation)
            response = web.StreamResponse(status=200, headers={"Content-Type": "application/octet-stream"})
            # The signed prepare result authenticates stream bytes; a body HMAC
            # cannot be emitted before a streamed response finishes.
            if operation:
                response.headers["X-Darak-Operation"] = operation
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
            finally:
                await upstream.release()
            await response.write_eof()
            return response
        finally:
            download_active = False

    async def backup_upload_handler(request):
        nonlocal upload_active
        operation = _operation(request)
        if request.get("body", b""):
            return _error(400, "invalid_request", operation)
        if upload_active:
            return _error(429, "busy", operation)
        try:
            if (set(request.query.keys()) != {"size", "sha256"}
                    or any(len(request.query.getall(name, [])) != 1 for name in ("size", "sha256"))):
                raise ValueError
            size_text, digest = request.query["size"], request.query["sha256"]
            if not re.fullmatch(r"0|[1-9][0-9]*", size_text) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
            size = int(size_text)
            if size > 8 * 1024**3 or request.content_length != size:
                return _error(400, "invalid_request", operation)
        except (ValueError, KeyError):
            return _error(400, "invalid_request", operation)
        upload_dir = getattr(client, "upload_dir", "/data/uploads")
        data_dir = os.path.dirname(upload_dir)
        if shutil.disk_usage(data_dir).free < size + 512 * 1024**2:
            return _error(409, "conflict", operation)
        upload_active = True
        temporary = None
        try:
            os.makedirs(upload_dir, mode=0o700, exist_ok=True)
            temp_path = os.path.join(upload_dir, (operation or "upload") + ".tar.partial")
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            temporary = temp_path
            os.fchmod(fd, 0o600)
            hasher, received = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as stream:
                async for chunk in request.content.iter_chunked(65536):
                    received += len(chunk)
                    if received > size:
                        return _error(400, "invalid_request", operation)
                    hasher.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if received != size or hasher.hexdigest() != digest:
                return _error(400, "invalid_request", operation)
            slug = await client.backup_upload(temporary)
            if not isinstance(slug, str) or not re.fullmatch(r"[0-9a-f]{8}", slug):
                return _error(502, "upstream_error", operation)
            return web.json_response({"ok": True, "operation": operation,
                                      "result": {"backup_slug": slug}})
        except Exception:
            return _error(502, "upstream_error", operation)
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            upload_active = False

    app = web.Application(middlewares=[boundary], client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/v1/commands/{name}", command_handler)
    app.router.add_get("/v1/backups/{slug}", backup_stream_handler)
    app.router.add_post("/v1/backups/upload", backup_upload_handler)
    app.router.add_get("/metrics", metrics_handler)
    return app
