"""Voice timers, alarms and reminders, kept per requesting phone and across restarts."""

import logging
import secrets
from datetime import datetime, timedelta

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from .ops import Full, NoPhone, OpRejected, valid_schedule_op, validate_service_op

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
STORE_KEY = "darak_voice.timers"
# ponytail: a timer overdue by more than 15 minutes at restart is dropped, not rung late.
MAX_OVERDUE = timedelta(minutes=15)
MAX_REMINDERS = 20
MAX_COMMANDS = 20
REMINDER_QUERY_LIMIT = 5
_MESSAGES = {"timer": "خلص التايمر.", "alarm": "المنبه! صار الوقت."}
_TAGS = {"timer": "darak_timer", "alarm": "darak_alarm"}
_REMINDER_TITLE = "تذكير من دارك"


def notify_service_for(hass, device_id) -> str | None:
    """Return the notify service of the companion phone behind device_id, if it is reachable."""
    if not isinstance(device_id, str) or not device_id:
        return None
    device = dr.async_get(hass).devices.get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != "mobile_app":
            continue
        name = (entry.data or {}).get("device_name")
        if not isinstance(name, str) or not name:
            return None
        service = "mobile_app_" + slugify(name)
        return service if hass.services.has_service("notify", service) else None
    return None


def _payload(message, tag, title="دارك"):
    return {
        "title": title,
        "message": message,
        "data": {
            "ttl": 0,
            "priority": "high",
            "channel": "alarm_stream",
            "tag": tag,
            "push": {
                "interruption-level": "critical",
                "sound": {"name": "default", "critical": 1, "volume": 1.0},
            },
        },
    }


class TimerManager:
    """Keep each phone's timer, alarm and reminders, schedule them, and ring the asking phone when due."""

    def __init__(self, hass, catalog_ids=lambda: frozenset(), sensitive_covers=lambda: frozenset()):
        self.hass = hass
        self._catalog_ids = catalog_ids
        self._sensitive_covers = sensitive_covers
        self._store = Store(hass, STORE_VERSION, STORE_KEY)
        # {device_id: {"timer"|"alarm": {"due", "service"}, "reminders": [{"due", "service", "text", "id"}]}}
        self._data = {}
        self._unsubs = {}  # {(device_id, kind) or (device_id, "reminder", id): cancel callable}

    async def async_start(self):
        stored = await self._store.async_load() or {}
        now = dt_util.utcnow()
        due_now = []
        for device_id, kinds in stored.items():
            if device_id == "commands":
                self._restore_commands(kinds, now, due_now)
                continue
            for kind, value in kinds.items():
                if kind == "reminders":
                    self._restore_reminders(device_id, value, now, due_now)
                    continue
                if kind in self._data.get(device_id, {}):
                    continue  # set in memory before start; that entry is already scheduled
                due = datetime.fromisoformat(value["due"])
                if now - due > MAX_OVERDUE:
                    continue
                self._data.setdefault(device_id, {})[kind] = value
                if due <= now:
                    due_now.append(self._fire(device_id, kind))
                else:
                    self._schedule(device_id, kind, due)
        await self._store.async_save(self._data)
        for job in due_now:
            await job

    def _restore_commands(self, stored, now, due_now):
        for entry in stored:
            due = datetime.fromisoformat(entry["due"])
            if now - due > MAX_OVERDUE or any(item["id"] == entry["id"] for item in self._commands()):
                continue
            self._commands().append(entry)
            if due <= now:
                due_now.append(self._fire_command(entry["id"]))
            else:
                self._schedule_command(entry["id"], due)

    def _commands(self):
        return self._data.setdefault("commands", [])

    async def async_schedule(self, message, catalog_ids=None):
        if not valid_schedule_op(message, self._catalog_ids() if catalog_ids is None else catalog_ids,
                                 self._sensitive_covers() if message.get("action") == "set" else ()):
            raise OpRejected()
        action = message["action"]
        if action == "set":
            commands = self._commands()
            if len(commands) >= MAX_COMMANDS:
                raise Full()
            due = dt_util.utcnow() + timedelta(seconds=message["seconds"])
            entry = {"id": secrets.token_hex(8), "due": due.isoformat(),
                     "label": message["label"].strip(), "calls": message["calls"]}
            commands.append(entry)
            await self._store.async_save(self._data)
            self._schedule_command(entry["id"], due)
            return {"due": entry["due"]}
        if action == "cancel":
            entries = self._commands()
            for entry in entries:
                self._cancel_command(entry["id"])
            self._data.pop("commands", None)
            await self._store.async_save(self._data)
            return {"cancelled": len(entries)}
        ordered = sorted(self._commands(), key=lambda entry: datetime.fromisoformat(entry["due"]))
        return {"pending": [{"label": entry["label"], "due": entry["due"]} for entry in ordered[:5]]}

    def _restore_reminders(self, device_id, stored, now, due_now):
        present = {entry["id"] for entry in self._reminders(device_id)}
        for entry in stored:
            if entry["id"] in present:
                continue  # set in memory before start; that entry is already scheduled
            due = datetime.fromisoformat(entry["due"])
            if now - due > MAX_OVERDUE:
                continue
            self._data.setdefault(device_id, {}).setdefault("reminders", []).append(entry)
            if due <= now:
                due_now.append(self._fire_reminder(device_id, entry["id"]))
            else:
                self._schedule_reminder(device_id, entry["id"], due)

    async def async_stop(self):
        for unsub in self._unsubs.values():
            unsub()
        self._unsubs.clear()

    async def async_handle(self, message, device_id) -> dict:
        service = notify_service_for(self.hass, device_id)
        if service is None:
            raise NoPhone()
        action, kind = message["action"], message["kind"]
        if kind == "reminder":
            return await self._handle_reminder(action, message, device_id, service)
        if action == "set":
            seconds = message["seconds"]
            due = dt_util.utcnow() + timedelta(seconds=seconds)
            self._cancel(device_id, kind)
            self._data.setdefault(device_id, {})[kind] = {"due": due.isoformat(), "service": service}
            await self._store.async_save(self._data)
            self._schedule(device_id, kind, due)
            return {"remaining": seconds}
        if action == "cancel":
            self._cancel(device_id, kind)
            if self._remove(device_id, kind) is None:
                return {"cancelled": False}
            await self._store.async_save(self._data)
            return {"cancelled": True}
        entry = self._data.get(device_id, {}).get(kind)
        if entry is None:
            return {"remaining": None}
        left = datetime.fromisoformat(entry["due"]) - dt_util.utcnow()
        return {"remaining": max(0, round(left.total_seconds()))}

    async def _handle_reminder(self, action, message, device_id, service):
        if action == "set":
            reminders = self._reminders(device_id)
            if len(reminders) >= MAX_REMINDERS:
                raise Full()
            seconds = message["seconds"]
            due = dt_util.utcnow() + timedelta(seconds=seconds)
            entry = {"due": due.isoformat(), "service": service,
                     "text": message["text"].strip(), "id": secrets.token_hex(6)}
            self._data.setdefault(device_id, {}).setdefault("reminders", []).append(entry)
            await self._store.async_save(self._data)
            self._schedule_reminder(device_id, entry["id"], due)
            return {"remaining": seconds}
        if action == "cancel":
            dropped = self._drop_reminders(device_id)
            for entry in dropped:
                self._cancel(device_id, "reminder", entry["id"])
            if not dropped:
                return {"cancelled": False}
            await self._store.async_save(self._data)
            return {"cancelled": True}
        now = dt_util.utcnow()
        ordered = sorted(self._reminders(device_id), key=lambda entry: datetime.fromisoformat(entry["due"]))
        return {"reminders": [
            {"remaining": max(0, round((datetime.fromisoformat(entry["due"]) - now).total_seconds())),
             "text": entry["text"]}
            for entry in ordered[:REMINDER_QUERY_LIMIT]
        ]}

    def _reminders(self, device_id):
        return self._data.get(device_id, {}).get("reminders", [])

    def _drop_reminders(self, device_id):
        phone = self._data.get(device_id, {})
        dropped = phone.pop("reminders", [])
        if not phone:
            self._data.pop(device_id, None)
        return dropped

    def _schedule(self, device_id, kind, due):
        async def fire(_now):
            await self._fire(device_id, kind)

        self._unsubs[(device_id, kind)] = async_track_point_in_utc_time(self.hass, fire, due)

    def _schedule_reminder(self, device_id, rid, due):
        async def fire(_now):
            await self._fire_reminder(device_id, rid)

        self._unsubs[(device_id, "reminder", rid)] = async_track_point_in_utc_time(self.hass, fire, due)

    def _schedule_command(self, command_id, due):
        async def fire(_now):
            await self._fire_command(command_id)

        self._unsubs[("commands", command_id)] = async_track_point_in_utc_time(self.hass, fire, due)

    def _cancel_command(self, command_id):
        unsub = self._unsubs.pop(("commands", command_id), None)
        if unsub:
            unsub()

    async def _fire_command(self, command_id):
        self._unsubs.pop(("commands", command_id), None)
        entry = next((item for item in self._commands() if item["id"] == command_id), None)
        if entry is None:
            return
        try:
            catalog_ids = self._catalog_ids()
            sensitive_covers = self._sensitive_covers()
        except Exception:
            _LOGGER.warning("Darak Voice scheduled command skipped because catalog lookup failed")
        else:
            for call in entry["calls"]:
                try:
                    domain, service, ids, data = validate_service_op(call, catalog_ids)
                    if (domain in {"lock", "valve"} or (domain, service) == ("button", "press")
                            or any(entity_id in sensitive_covers for entity_id in ids)):
                        raise OpRejected()
                except (OpRejected, TypeError, AttributeError):
                    _LOGGER.warning("Darak Voice scheduled call skipped domain=%s service=%s",
                                    call.get("domain"), call.get("service"))
                    continue
                try:
                    await self.hass.services.async_call(
                        domain, service, {"entity_id": list(ids), **data}, blocking=False
                    )
                except Exception as err:
                    _LOGGER.warning("Darak Voice scheduled call failed domain=%s service=%s error=%s",
                                    domain, service, type(err).__name__)
        self._commands().remove(entry)
        if not self._commands():
            self._data.pop("commands", None)
        await self._store.async_save(self._data)

    def _cancel(self, device_id, kind, rid=None):
        key = (device_id, kind) if rid is None else (device_id, kind, rid)
        unsub = self._unsubs.pop(key, None)
        if unsub is not None:
            unsub()

    def _remove(self, device_id, kind):
        kinds = self._data.get(device_id, {})
        entry = kinds.pop(kind, None)
        if not kinds:
            self._data.pop(device_id, None)
        return entry

    async def _fire(self, device_id, kind):
        self._unsubs.pop((device_id, kind), None)
        entry = self._remove(device_id, kind)
        if entry is None:
            return
        await self._store.async_save(self._data)
        await self._notify(entry["service"], _payload(_MESSAGES[kind], _TAGS[kind]))

    async def _fire_reminder(self, device_id, rid):
        self._unsubs.pop((device_id, "reminder", rid), None)
        phone = self._data.get(device_id, {})
        reminders = phone.get("reminders", [])
        entry = next((item for item in reminders if item["id"] == rid), None)
        if entry is None:
            return
        reminders.remove(entry)
        if not reminders:
            phone.pop("reminders", None)
        if not phone:
            self._data.pop(device_id, None)
        await self._store.async_save(self._data)
        await self._notify(entry["service"], _payload(
            entry["text"], "darak_reminder_" + rid, title=_REMINDER_TITLE))

    async def _notify(self, service, payload):
        try:
            await self.hass.services.async_call("notify", service, payload, blocking=False)
        except Exception as err:
            _LOGGER.warning("Darak Voice timer notify failed error=%s", type(err).__name__)
