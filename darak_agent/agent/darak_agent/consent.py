"""Customer consent for short-lived technician sessions."""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .enrollment import write_private_json
from .ha_auth import tech_user_disable, tech_user_enable
from .tech_sessions import _key, ssh_close, ssh_open

LOGGER = logging.getLogger("darak_agent.consent")
WINDOW = timedelta(minutes=10)
# One lock serializes every consent state change (hub commands, sweep, ingress).
_STATE_LOCK = asyncio.Lock()
_STATES = {"pending", "active", "ended", "declined", "expired", "cancelled"}
_PUSH_TASKS = set()
_PUSH_LOCK = None
_PUSH_LOOP = None


def _now(client):
    return (getattr(client, "session_clock", None) or (lambda: datetime.now(timezone.utc)))()


def _iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(client):
    return Path(getattr(client, "request_path", Path(client.tech_path).with_name("session-request.json")))


def _read(client):
    try:
        value = json.loads(_path(client).read_text())
        state = value.get("state") if isinstance(value, dict) else None
        if (not isinstance(state, str) or state not in _STATES
                or state == "pending" and not _valid_time(value.get("approval_expires_at"))):
            raise ValueError
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        LOGGER.warning('{"event": "support_request_unreadable"}')
        return None


def _valid_time(value):
    try:
        _record_time(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _save(client, record):
    write_private_json(_path(client), record)


def _drop_secrets(record):
    for key in ("password", "public_key", "approval_token"):
        record.pop(key, None)


def _record_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def session_mode(client):
    from .commands import CommandError
    try:
        data = await client.get_json("/addons/self/info")
        options = data.get("options") if isinstance(data, dict) else None
        mode = options.get("support_sessions", "allow") if isinstance(options, dict) else None
        if mode not in {"allow", "ask", "block"}:
            raise ValueError
        return mode
    except Exception:
        raise CommandError("upstream_unavailable") from None


async def _notification(client, kind, *, minutes=None, ssh=False):
    titles = {
        "pending": "Darak support request · طلب دعم من دارَك",
        "active": "Darak support session active · جلسة دعم دارَك فعّالة",
        "ended": "Darak support session ended · انتهت جلسة دعم دارَك",
        "declined": "Darak support request declined · تم رفض طلب دعم دارَك",
        "expired": "Darak support request expired · انتهت صلاحية طلب دعم دارَك",
    }
    try:
        info = await client.get_json("/addons/self/info")
        slug = info.get("slug") if isinstance(info, dict) and isinstance(info.get("slug"), str) else None
    except Exception:
        slug = None
    link = f"/hassio/ingress/{slug}" if slug else None
    msg = _bell_text(kind, minutes, ssh, link)
    try:
        await client.admin_ws([{"type": "call_service", "domain": "persistent_notification", "service": "create",
                                "service_data": {"notification_id": "darak_support_session", "title": titles[kind], "message": msg}}])
    except Exception:
        LOGGER.warning('{"event": "support_notification_failed", "kind": "%s"}', kind)
    task = asyncio.get_running_loop().create_task(
        _run_push_notification(client, kind, minutes, ssh, link, titles[kind]))
    _PUSH_TASKS.add(task)
    task.add_done_callback(_PUSH_TASKS.discard)


async def drain_pushes():
    while _PUSH_TASKS:
        await asyncio.gather(*_PUSH_TASKS, return_exceptions=True)


def _bell_text(kind, minutes, ssh, link):
    if kind == "pending":
        en = ", including SSH" if ssh else ""
        ar = "، بما في ذلك SSH" if ssh else ""
        msg = (f"Darak support is asking to access your Home Assistant for up to {minutes} minutes{en}. "
               f"The request expires in 10 minutes.\n\nيطلب فريق دعم دارَك الوصول إلى نظام منزلك لمدة أقصاها {minutes} دقيقة{ar}. "
               "ينتهي الطلب خلال 10 دقائق.")
        if link:
            msg = (f"Darak support is asking to access your Home Assistant for up to {minutes} minutes{en}. "
                   f"[Approve or decline]({link}). The request expires in 10 minutes.\n\n"
                   f"يطلب فريق دعم دارَك الوصول إلى نظام منزلك لمدة أقصاها {minutes} دقيقة{ar}. "
                   f"[وافق أو ارفض]({link}). ينتهي الطلب خلال 10 دقائق.")
    elif kind == "active":
        en = ", including SSH" if ssh else ""
        ar = "، بما في ذلك SSH" if ssh else ""
        msg = (f"A Darak technician can access your Home Assistant{en} for up to {minutes} minutes. Access ends automatically.\n\n"
               f"يستطيع فني من دارَك الوصول إلى نظام منزلك{ar} لمدة أقصاها {minutes} دقيقة، وينتهي الوصول تلقائيًا.")
        if link:
            msg = (f"A Darak technician can access your Home Assistant{en} for up to {minutes} minutes. "
                   f"Access ends automatically. [End it now]({link}).\n\n"
                   f"يستطيع فني من دارَك الوصول إلى نظام منزلك{ar} لمدة أقصاها {minutes} دقيقة، وينتهي الوصول تلقائيًا. "
                   f"[أنهِ الجلسة الآن]({link}).")
    else:
        msg = {
            "ended": "The Darak technician's access has been removed.\n\nتمت إزالة وصول فني دارَك.",
            "declined": "You declined the support request. Darak has no access.\n\nرفضت طلب الدعم. لا يملك دارَك أي وصول.",
            "expired": "The support request was not answered in time. Darak has no access.\n\nلم تتم الإجابة على طلب الدعم في الوقت المحدد. لا يملك دارَك أي وصول.",
        }[kind]
    return msg


def _push_text(kind, minutes, ssh, link):
    msg = _bell_text(kind, minutes, ssh, link)
    if link and kind == "pending":
        msg = msg.replace(f"[Approve or decline]({link})", "Tap to approve or decline")
        msg = msg.replace(f"[وافق أو ارفض]({link})", "اضغط للموافقة أو الرفض")
    elif link and kind == "active":
        msg = msg.replace(f"[End it now]({link})", "Tap to end it now")
        msg = msg.replace(f"[أنهِ الجلسة الآن]({link})", "اضغط لإنهاء الجلسة الآن")
    return msg


async def _push_notification(client, kind, minutes, ssh, link, title):
    global _PUSH_LOCK, _PUSH_LOOP
    loop = asyncio.get_running_loop()
    if _PUSH_LOOP is not loop:
        _PUSH_LOCK, _PUSH_LOOP = asyncio.Lock(), loop
    async with _PUSH_LOCK:
        await _send_push_notification(client, kind, minutes, ssh, link, title)


async def _run_push_notification(client, kind, minutes, ssh, link, title):
    try:
        await _push_notification(client, kind, minutes, ssh, link, title)
    except Exception:
        LOGGER.warning(json.dumps({"event": "support_push_failed", "kind": kind, "stage": "unexpected"}))


async def _send_push_notification(client, kind, minutes, ssh, link, title):
    try:
        services, = await client.admin_ws([{"type": "get_services"}])
        notify = services.get("notify") if isinstance(services, dict) else None
        if not isinstance(notify, dict):
            raise ValueError
        names = sorted(name for name in notify if isinstance(name, str) and name.startswith("mobile_app_")
                       and re.fullmatch(r"[a-z0-9_]{1,128}", name))[:20]
    except Exception:
        LOGGER.warning(json.dumps({"event": "support_push_failed", "kind": kind, "stage": "discover"}))
        return
    data = {"tag": "darak_support_session", "group": "darak_support"}
    if link:
        data.update(url=link, clickAction=link)
    if kind in {"pending", "active"}:
        data.update(push={"interruption-level": "time-sensitive"}, priority="high", ttl=0)
    service_data = {"title": title, "message": _push_text(kind, minutes, ssh, link), "data": data}
    sent = failed = 0
    for name in names:
        try:
            await client.admin_ws([{"type": "call_service", "domain": "notify", "service": name,
                                    "service_data": service_data}])
            sent += 1
        except Exception:
            failed += 1
    if failed:
        LOGGER.warning(json.dumps({"event": "support_push_failed", "kind": kind, "stage": "send",
                                   "sent": sent, "failed": failed}))
    else:
        LOGGER.info(json.dumps({"event": "support_push_sent", "kind": kind, "sent": sent, "failed": 0}))


async def _dismiss(client):
    try:
        await client.admin_ws([{"type": "call_service", "domain": "persistent_notification", "service": "dismiss",
                                "service_data": {"notification_id": "darak_support_session"}}])
    except Exception:
        LOGGER.warning('{"event": "support_notification_failed", "kind": "dismiss"}')


async def _activate(client, password, minutes, public_key):
    expires_at = _iso(_now(client) + timedelta(minutes=minutes))
    await tech_user_enable(client, {"password": password, "expires_at": expires_at})
    ssh = None
    if public_key:
        try:
            ssh = await ssh_open(client, {"public_key": public_key, "expires_at": expires_at})
        except Exception:
            try:
                await tech_user_disable(client)
            except Exception:
                pass
            raise
    await _notification(client, "active", minutes=minutes, ssh=bool(public_key))
    return {"expires_at": expires_at, "ssh": bool(public_key), "user": ssh["user"] if public_key else None}


def _active_session(client):
    # An SSH-only session (allow mode ssh_open) has no tech file, so check both.
    for path in (client.tech_path, getattr(client, "ssh_path", None)):
        try:
            state = json.loads(Path(path).read_text())
            if _record_time(state["expires_at"]) > _now(client):
                return True
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    return False


def _key_hash(public_key):
    return hashlib.sha256(public_key.encode()).hexdigest()


async def revoke_session(client):
    """End SSH, then the technician user, exactly like the expiry sweep. Never raises."""
    for operation in (ssh_close, disable_with_consent):
        try:
            await operation(client)
        except Exception:
            LOGGER.warning('{"event": "support_session_revoke_failed", "step": "%s"}', operation.__name__)


async def end_ssh_session(client):
    """Sweep expiry of the SSH key: close it, and end an SSH-only record (no tech user) with its notice."""
    result = await ssh_close(client)
    if not Path(client.tech_path).exists() and (_read(client) or {}).get("state") == "active":
        await disable_with_consent(client)
    return result


async def end_if_blocked(client):
    """Called by the sweep: the customer switched to block, so end whatever is live."""
    record = _read(client)
    live = (record and record.get("state") in {"active", "pending"}
            or Path(client.tech_path).exists() or Path(client.ssh_path).exists())
    if live and await session_mode(client) == "block":
        await revoke_session(client)
        return True
    return False


async def expire_pending(client):
    async with _STATE_LOCK:
        return await _expire_pending_locked(client)


async def _expire_pending_locked(client):
    record = _read(client)
    if record and record.get("state") == "pending" and _record_time(record["approval_expires_at"]) <= _now(client):
        record["state"] = "expired"
        record["resolved_at"] = _iso(_now(client))
        record["end_token"] = None
        _drop_secrets(record)
        _save(client, record)
        await _dismiss(client)
        await _notification(client, "expired")
        return True
    return False


async def session_start(client, params):
    async with _STATE_LOCK:
        return await _session_start_locked(client, params)


async def _session_start_locked(client, params):
    from .commands import CommandError
    mode = await session_mode(client)
    await _expire_pending_locked(client)
    if _active_session(client):
        raise CommandError("conflict")
    if mode == "block":
        raise CommandError("customer_blocked")
    if mode == "ask":
        current = _read(client)
        if current and current.get("state") == "pending":
            raise CommandError("conflict")
        now = _now(client)
        request_id = uuid4().hex
        record = {"request_id": request_id, "state": "pending", "created_at": _iso(now),
                  "approval_expires_at": _iso(now + WINDOW), "minutes": params["minutes"],
                  "ssh": bool(params.get("public_key")), "password": params["password"],
                  "public_key": params.get("public_key"), "approval_token": secrets.token_urlsafe(32),
                  "end_token": None, "expires_at": None, "user": None, "resolved_at": None}
        _save(client, record)
        await _notification(client, "pending", minutes=params["minutes"], ssh=record["ssh"])
        return {"state": "pending_approval", "request_id": request_id,
                "approval_expires_at": record["approval_expires_at"]}
    result = await _activate(client, params["password"], params["minutes"], params.get("public_key"))
    record = {"request_id": uuid4().hex, "state": "active", "created_at": _iso(_now(client)),
              "approval_expires_at": None, "minutes": params["minutes"], "ssh": result["ssh"],
              "end_token": secrets.token_urlsafe(32), "expires_at": result["expires_at"],
              "user": result["user"], "resolved_at": _iso(_now(client))}
    _save(client, record)
    return {"state": "active", **result}


async def session_status(client, params):
    async with _STATE_LOCK:
        return await _session_status_locked(client, params)


async def _session_status_locked(client, params):
    await _expire_pending_locked(client)
    record = _read(client)
    if not record or record.get("request_id") != params["request_id"]:
        return {"state": "unknown"}
    state = record.get("state")
    if state == "pending" and _record_time(record["approval_expires_at"]) <= _now(client):
        await _expire_pending_locked(client)
        record = _read(client)
        state = record["state"]
    if state == "active" and not _active_session(client):
        record["state"] = state = "ended"
        record["resolved_at"] = _iso(_now(client))
        record["end_token"] = None
        _drop_secrets(record)
        _save(client, record)
    return {key: record.get(key) for key in ("state", "request_id", "approval_expires_at", "expires_at", "ssh", "user")}


async def _check_token(client, request_id, token):
    from .commands import CommandError
    record = _read(client)
    if (not record or record.get("state") != "pending" or record.get("request_id") != request_id
            or not isinstance(token, str) or not isinstance(record.get("approval_token"), str)
            or not hmac.compare_digest(token, record["approval_token"])):
        raise CommandError("conflict")
    if _record_time(record["approval_expires_at"]) <= _now(client):
        await _expire_pending_locked(client)
        raise CommandError("conflict")
    return record


async def approve(client, request_id, token):
    async with _STATE_LOCK:
        return await _approve_locked(client, request_id, token)


async def _approve_locked(client, request_id, token):
    record = await _check_token(client, request_id, token)
    if await session_mode(client) == "block":
        # The customer switched to block after the request was made.
        record.update(state="declined", resolved_at=_iso(_now(client)))
        _drop_secrets(record)
        _save(client, record)
        await _dismiss(client)
        await _notification(client, "declined")
        from .commands import CommandError
        raise CommandError("customer_blocked")
    try:
        result = await _activate(client, record["password"], record["minutes"], record.get("public_key"))
    except Exception:
        record["state"] = "cancelled"
        record["resolved_at"] = _iso(_now(client))
        _drop_secrets(record)
        _save(client, record)
        raise
    record.update(state="active", expires_at=result["expires_at"], user=result["user"], ssh=result["ssh"],
                  resolved_at=_iso(_now(client)), end_token=secrets.token_urlsafe(32))
    if record.get("public_key"):
        record["approved_key"] = _key_hash(record["public_key"])
    _drop_secrets(record)
    _save(client, record)
    return {"state": "active", **result}


async def decline(client, request_id, token):
    async with _STATE_LOCK:
        return await _decline_locked(client, request_id, token)


async def _decline_locked(client, request_id, token):
    record = await _check_token(client, request_id, token)
    record.update(state="declined", resolved_at=_iso(_now(client)))
    _drop_secrets(record)
    _save(client, record)
    await _dismiss(client)
    await _notification(client, "declined")


async def end_now(client, end_token):
    async with _STATE_LOCK:
        return await _end_now_locked(client, end_token)


async def _end_now_locked(client, end_token):
    from .commands import CommandError
    record = _read(client)
    if (not record or record.get("state") != "active" or not isinstance(end_token, str)
            or not isinstance(record.get("end_token"), str)
            or not hmac.compare_digest(end_token, record["end_token"])):
        raise CommandError("conflict")
    for operation, label in ((ssh_close, "ssh"), (_disable_with_consent_locked, "technician")):
        try:
            await operation(client)
        except Exception:
            LOGGER.warning('{"event": "support_session_end_failed", "kind": "%s"}', label)
            if label == "technician":
                await _dismiss(client)
    record.update(state="ended", resolved_at=_iso(_now(client)))
    record["end_token"] = None
    _drop_secrets(record)
    _save(client, record)


def read_request(client):
    """Return a copy for ingress rendering; this may include approval_token/end_token form values."""
    record = _read(client)
    if record is None:
        return None
    return {key: value for key, value in record.items() if key not in {"password", "public_key"}}


async def disable_with_consent(client, params=None):
    async with _STATE_LOCK:
        return await _disable_with_consent_locked(client, params)


async def _disable_with_consent_locked(client, params=None):
    result = await tech_user_disable(client)
    record = _read(client)
    if record and record.get("state") == "pending":
        record["state"] = "cancelled"
        record["resolved_at"] = _iso(_now(client))
        record["end_token"] = None
        _drop_secrets(record)
        _save(client, record)
        await _dismiss(client)
    elif record and record.get("state") == "active":
        record["state"] = "ended"
        record["resolved_at"] = _iso(_now(client))
        record["end_token"] = None
        _drop_secrets(record)
        _save(client, record)
        await _dismiss(client)
        await _notification(client, "ended")
    return result


async def gated_tech_user_enable(client, params):
    async with _STATE_LOCK:
        return await _gated_tech_user_enable_locked(client, params)


async def _gated_tech_user_enable_locked(client, params):
    from .commands import CommandError
    mode = await session_mode(client)
    if mode == "block":
        raise CommandError("customer_blocked")
    if mode == "ask":
        raise CommandError("approval_required")
    result = await tech_user_enable(client, params)
    expiry = _record_time(params["expires_at"])
    minutes = max(1, math.ceil((expiry - _now(client)).total_seconds() / 60))
    _save(client, {"request_id": uuid4().hex, "state": "active", "created_at": _iso(_now(client)),
                   "approval_expires_at": None, "minutes": minutes, "ssh": False,
                   "end_token": secrets.token_urlsafe(32), "expires_at": params["expires_at"],
                   "user": None, "resolved_at": _iso(_now(client))})
    await _notification(client, "active", minutes=minutes, ssh=False)
    return result


async def gated_ssh_open(client, params):
    async with _STATE_LOCK:
        return await _gated_ssh_open_locked(client, params)


async def _gated_ssh_open_locked(client, params):
    from .commands import CommandError
    mode = await session_mode(client)
    record = _read(client)
    if mode == "block":
        raise CommandError("customer_blocked")
    active = bool(record and record.get("state") == "active")
    if mode == "ask" and not (active and record.get("ssh") is True
                              and record.get("approved_key") == _key_hash(params["public_key"])):
        # Ask mode: only the key the customer approved may be installed.
        raise CommandError("approval_required")
    if active and _record_time(params["expires_at"]) > _record_time(record["expires_at"]):
        raise CommandError("approval_required")
    result = await ssh_open(client, params)
    minutes = max(1, math.ceil((_record_time(params["expires_at"]) - _now(client)).total_seconds() / 60))
    if active:
        record["ssh"] = True
        record["user"] = result["user"]
        if mode == "allow":
            record["expires_at"] = params["expires_at"]
            record["minutes"] = minutes
        _save(client, record)
        await _notification(client, "active", minutes=record["minutes"], ssh=True)
    else:
        # Allow mode with no session: SSH still gets a visible record, an end token and a notice.
        _save(client, {"request_id": uuid4().hex, "state": "active", "created_at": _iso(_now(client)),
                       "approval_expires_at": None, "minutes": minutes, "ssh": True,
                       "end_token": secrets.token_urlsafe(32), "expires_at": params["expires_at"],
                       "user": result["user"], "resolved_at": _iso(_now(client))})
        await _notification(client, "active", minutes=minutes, ssh=True)
    return result
