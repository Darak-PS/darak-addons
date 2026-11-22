"""Home Assistant ingress page for technician session consent."""

import html
import logging
from datetime import datetime, timezone

from aiohttp import web

from . import consent
from .commands import CommandError
from .ha_auth import MANAGED_USERNAME, TECH_DISPLAY_NAME, TECH_USERNAME, USERNAME, managed_user_is_stored

LOGGER = logging.getLogger("darak_agent.ingress")
TRUSTED_SOURCE = "172.30.32.2"
MAX_BODY_BYTES = 4096
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'",
    "X-Frame-Options": "SAMEORIGIN",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
NOTICES = {
    "conflict": ("This request is no longer waiting for an answer.", "لم يعد هذا الطلب بانتظار إجابة."),
    "error": ("Something went wrong. Try again.", "حدث خطأ. حاول مرة أخرى."),
    "signed_in": ("Approval needs a signed-in Home Assistant user.",
                  "تتطلب الموافقة مستخدماً مسجّلاً في Home Assistant."),
    "darak_account": ("Darak accounts can't approve support requests.",
                       "لا يمكن لحسابات دارَك الموافقة على طلبات الدعم."),
}
TERMINAL = {
    "declined": ("declined", "مرفوض"), "expired": ("expired", "منتهي الصلاحية"),
    "ended": ("ended", "منتهٍ"), "cancelled": ("cancelled", "ملغى"),
}


def _e(value):
    return html.escape(str(value), quote=True)


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%H:%M UTC")


def _page(mode, record, notice=None, show_actions=True):
    mode = mode if mode in {"allow", "ask", "block"} else "unknown"
    mode_ar = {"allow": "السماح", "ask": "السؤال", "block": "الحظر", "unknown": "غير معروف"}[mode]
    parts = ['<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>Darak support</title><style>body{font:16px sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#202124}section{border:1px solid #bbb;border-radius:.6rem;padding:1rem;margin:1rem 0}button{font:inherit;padding:.6rem 1rem;margin:.3rem}form{display:inline-block} .notice{background:#fff3cd;padding:.8rem} [dir=rtl]{text-align:right}</style></head><body>']
    parts += ['<section><h1>Darak</h1>', f'<p>Support sessions: <strong>{_e(mode)}</strong></p>',
              '<p>To change this, open the Darak Agent add-on&#x27;s Configuration tab.</p></section>',
              '<section dir="rtl" lang="ar"><h1>دارَك</h1>',
              f'<p>جلسات الدعم: <strong>{_e(mode_ar)}</strong></p>',
              '<p>لتغيير ذلك، افتح تبويب الإعدادات في إضافة Darak Agent.</p></section>']
    if notice:
        en, ar = notice
        parts += [f'<p class="notice">{_e(en)}</p>', f'<p class="notice" dir="rtl" lang="ar">{_e(ar)}</p>']
    if record and record.get("state") == "pending":
        deadline = _time(record["approval_expires_at"])
        ssh_en, ssh_ar = ("yes", "نعم") if record.get("ssh") else ("no", "لا")
        parts += ['<section><h2>Support request</h2>',
                  f'<p>Requested duration: {_e(record.get("minutes", "unknown"))} minutes; SSH: {ssh_en}; approval deadline: {_e(deadline)}.</p>',
                  '<p dir="rtl" lang="ar">مدة الوصول المطلوبة: ', _e(record.get("minutes", "unknown")),
                  f' دقيقة؛ SSH: {ssh_ar}؛ مهلة الموافقة: {_e(deadline)}.</p>']
        if show_actions:
            parts += ['<form method="post" action="approve"><input type="hidden" name="request_id" value="',
                  _e(record.get("request_id", "")), '"><input type="hidden" name="token" value="',
                  _e(record.get("approval_token", "")), '"><button>Approve / وافق</button></form>',
                  '<form method="post" action="decline"><input type="hidden" name="request_id" value="',
                  _e(record.get("request_id", "")), '"><input type="hidden" name="token" value="',
                  _e(record.get("approval_token", "")), '"><button>Decline / ارفض</button></form>']
        parts.append('</section>')
    elif record and record.get("state") == "active":
        expiry = _time(record["expires_at"])
        ssh_en, ssh_ar = (", including SSH", "، بما في ذلك SSH") if record.get("ssh") else ("", "")
        parts += ['<section><p>A Darak technician has access until ', _e(expiry), _e(ssh_en), '.</p>',
                  '<p dir="rtl" lang="ar">يستطيع فني من دارَك الوصول حتى ', _e(expiry), _e(ssh_ar), '.</p>']
        if show_actions:
            parts += ['<form method="post" action="end"><input type="hidden" name="token" value="',
                      _e(record.get("end_token", "")), '"><button>End now / أنهِ الآن</button></form>']
        parts.append('</section>')
    else:
        parts += ['<section><p>No support request.</p><p dir="rtl" lang="ar">لا يوجد طلب دعم.</p>']
        if record and record.get("state") in TERMINAL:
            state_en, state_ar = TERMINAL[record["state"]]
            resolved = record.get("resolved_at")
            when = _time(resolved) if resolved else "unknown"
            parts += [f'<p>Last state: {_e(state_en)} at {_e(when)}.</p>',
                      f'<p dir="rtl" lang="ar">الحالة الأخيرة: {_e(state_ar)} عند {_e(when)}.</p>']
        parts.append('</section>')
    parts.append('</body></html>')
    return ''.join(parts)


async def _render(client, *, status=200, notice=None, show_actions=True):
    try:
        mode = await consent.session_mode(client)
    except Exception:
        mode = "unknown"
    await consent.expire_pending(client)
    record = consent.read_request(client)
    return web.Response(text=_page(mode, record, notice, show_actions), status=status,
                        content_type="text/html", charset="utf-8")


async def _form(request, expected):
    if request.content_type != "application/x-www-form-urlencoded":
        raise ValueError
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise ValueError
    try:
        data = await request.post()
    except (web.HTTPException, ValueError, UnicodeDecodeError):
        raise ValueError from None
    if set(data.keys()) != set(expected):
        raise ValueError
    result = {}
    for field in expected:
        values = data.getall(field, [])
        if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > 256:
            raise ValueError
        result[field] = values[0]
    return result


async def _is_household_user(request, client):
    user_id = request.headers.get("X-Remote-User-Id", "")
    username = request.headers.get("X-Remote-User-Name", "").casefold()
    if not user_id:
        return "signed_in", 403
    if username in {USERNAME.casefold(), TECH_USERNAME.casefold()}:
        return "darak_account", 403
    try:
        users, = await client.admin_ws([{"type": "config/auth/list"}])
    except Exception:
        return "error", 502
    if not isinstance(users, list):
        return "signed_in", 403
    user = next((item for item in users if isinstance(item, dict) and item.get("id") == user_id), None)
    if not user:
        return "signed_in", 403
    credentials = user.get("credentials", [])
    if not isinstance(credentials, list):
        return "signed_in", 403
    darak_credential = any(
        isinstance(credential, dict) and isinstance(credential.get("username"), str)
        and credential["username"].casefold() in {USERNAME.casefold(), TECH_USERNAME.casefold()}
        for credential in credentials)
    darak_username = user.get("username")
    if (user.get("system_generated") is True or user.get("name") == TECH_DISPLAY_NAME or darak_credential
            or (isinstance(darak_username, str)
                and darak_username.casefold() in {USERNAME.casefold(), TECH_USERNAME.casefold()})):
        return "darak_account", 403
    managed_username = (isinstance(darak_username, str) and MANAGED_USERNAME.fullmatch(darak_username))
    managed_credential = any(isinstance(credential, dict) and isinstance(credential.get("username"), str)
                             and MANAGED_USERNAME.fullmatch(credential["username"])
                             for credential in credentials)
    try:
        if managed_username or managed_credential or managed_user_is_stored(user_id):
            return "darak_account", 403
    except Exception:
        return "darak_account", 403
    return None


def create_ingress_app(client):
    @web.middleware
    async def boundary(request, handler):
        if request.remote != TRUSTED_SOURCE:
            response = web.Response(status=403, text="Forbidden")
        else:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = exc
        response.headers.update(HEADERS)
        return response

    async def index(request):
        return await _render(client)

    async def not_found(request):
        return web.Response(status=404, text="Not found")

    async def action(request):
        name = request.path.lstrip("/")
        expected = ("token",) if name == "end" else ("request_id", "token")
        try:
            values = await _form(request, expected)
        except ValueError:
            return web.Response(status=400, text="Bad request")
        if name == "approve":
            identity = await _is_household_user(request, client)
            if identity:
                notice, status = identity
                if status == 502:
                    LOGGER.info('{"event": "ingress_action_failed", "action": "approve"}')
                return await _render(client, status=status, notice=NOTICES[notice], show_actions=False)
        try:
            if name == "approve":
                await consent.approve(client, values["request_id"], values["token"])
                event = "approved"
            elif name == "decline":
                await consent.decline(client, values["request_id"], values["token"])
                event = "declined"
            else:
                await consent.end_now(client, values["token"])
                event = "ended"
        except CommandError as exc:
            if exc.code == "conflict":
                return await _render(client, status=409, notice=NOTICES["conflict"])
            LOGGER.info('{"event": "ingress_action_failed", "action": "%s"}', name)
            return await _render(client, status=502, notice=NOTICES["error"])
        except Exception:
            LOGGER.info('{"event": "ingress_action_failed", "action": "%s"}', name)
            return await _render(client, status=502, notice=NOTICES["error"])
        LOGGER.info('{"event": "support_request_%s"}', event)
        raise web.HTTPSeeOther("./")

    app = web.Application(middlewares=[boundary], client_max_size=MAX_BODY_BYTES)
    app.router.add_get("/", index, allow_head=False)
    app.router.add_post("/approve", action, name="approve")
    app.router.add_post("/decline", action, name="decline")
    app.router.add_post("/end", action, name="end")
    app.router.add_route("*", "/{tail:.*}", not_found)
    return app
