"""Pure protocol validation helpers for the Darak Voice service."""

import json
import math
import re
import time
import unicodedata
import wave
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from io import BytesIO

TOKEN_RE = re.compile(r"^dvt1\.[A-Za-z0-9_-]{43}$")
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
PROTOCOL_VERSION = 1
MAX_TEXT = 512
MAX_CATALOG = 3000
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
VERIFY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_AUDIO_BYTES = 2 * 1024 * 1024
STREAM_CHUNK_BYTES = 2048
UTTERANCE_HEADER = "X-Darak-Utterance-ID"
_utterance = ContextVar("darak_voice_utterance", default=None)


def new_utterance_id() -> str:
    return uuid.uuid4().hex


def remember_utterance(utterance_id, transcript, *, clock=time.monotonic):
    _utterance.set((utterance_id, transcript, clock()))


def take_utterance(text, *, clock=time.monotonic, max_age=120.0) -> str | None:
    return take_utterance_status(text, clock=clock, max_age=max_age)[0]


def take_utterance_status(text, *, clock=time.monotonic, max_age=120.0) -> tuple[str | None, str, int | None]:
    """Take a same-task STT correlation once and report why it did not match."""
    value = _utterance.get()
    _utterance.set(None)
    if value is None:
        return None, "none", None
    utterance_id, transcript, created = value
    age = clock() - created
    if age >= max_age:
        return None, "stale", None
    if transcript.strip() != text.strip():
        return None, "text_mismatch", None
    return utterance_id, "matched", max(0, int(age * 1000))


def stt_request_headers(auth_headers: dict, utterance_id: str | None) -> dict:
    headers = {**auth_headers, "Content-Type": "audio/wav"}
    if utterance_id is not None:
        headers[UTTERANCE_HEADER] = utterance_id
    return headers


def stream_start_message(language, utterance_id) -> dict:
    message = {
        "type": "start", "version": 1, "language": language,
        "format": {"rate": 16000, "width": 2, "channels": 1},
    }
    if utterance_id is not None:
        message["utterance_id"] = utterance_id
    return message


def parse_stream_message(text: str) -> dict | None:
    """Parse one strict server message from the STT stream protocol."""
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        return None
    kind = value["type"]
    keys = {
        "ready": {"type", "mode"},
        "endpoint": {"type", "reason", "at_ms"},
        "result": {"type", "text", "language", "duration_ms", "vocabulary"},
        "error": {"type", "code"},
    }
    if kind not in keys or set(value) != keys[kind]:
        return None
    if kind == "ready":
        return value if isinstance(value["mode"], str) and value["mode"] in {"off", "observe", "on", "stream"} else None
    if kind == "endpoint":
        at_ms = value["at_ms"]
        return value if isinstance(value["reason"], str) and value["reason"] in {"background", "silence", "complete"} and type(at_ms) is int and 0 <= at_ms <= 120000 else None
    if kind == "result":
        duration = value["duration_ms"]
        vocabulary = value["vocabulary"]
        return value if (
            isinstance(value["text"], str) and len(value["text"]) <= 4096
            and isinstance(value["language"], str) and value["language"] in {"ar", "en"}
            and type(duration) is int and duration >= 0
            and isinstance(vocabulary, str)
            and (vocabulary == "none" or re.fullmatch(r"[a-f0-9]{16,64}", vocabulary))
        ) else None
    return value if isinstance(value["code"], str) and re.fullmatch(r"[a-z0-9_]{1,40}", value["code"]) else None

CATALOG_DOMAINS = frozenset(
    {
        "binary_sensor", "calendar", "climate", "cover", "fan", "input_boolean", "light",
        "lock", "media_player", "person", "scene", "script", "sensor", "sun", "switch",
        "todo", "weather", "vacuum", "water_heater", "humidifier", "valve", "button",
    }
)
ALLOWED_SERVICES = {
    "climate": frozenset({"turn_on", "turn_off", "set_temperature", "set_hvac_mode",
                           "set_fan_mode", "set_swing_mode"}),
    "cover": frozenset({"open_cover", "close_cover", "stop_cover", "set_cover_position"}),
    "fan": frozenset({"turn_on", "turn_off", "toggle", "set_percentage", "oscillate"}),
    "vacuum": frozenset({"start", "pause", "stop", "return_to_base", "clean_area",
                          "darak_set_modes"}),
    "water_heater": frozenset({"turn_on", "turn_off", "set_temperature"}),
    "humidifier": frozenset({"turn_on", "turn_off", "set_humidity"}),
    "valve": frozenset({"open_valve", "close_valve", "stop_valve"}),
    "button": frozenset({"press"}),
    "lock": frozenset({"lock", "unlock"}),
    "input_boolean": frozenset({"turn_on", "turn_off", "toggle"}),
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "media_player": frozenset(
        {
            "turn_on", "turn_off", "toggle", "media_stop", "media_play", "media_pause",
            "media_next_track", "media_previous_track", "volume_set", "volume_up",
            "volume_down", "volume_mute",
        }
    ),
    "scene": frozenset({"turn_on"}),
    "script": frozenset({"turn_on"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
    "todo": frozenset({"add_item", "update_item", "remove_item"}),
}
READ_SERVICES = {
    ("weather", "get_forecasts"): frozenset({"type"}),
    ("todo", "get_items"): frozenset({"status"}),
    ("calendar", "get_events"): frozenset({"start_date_time", "end_date_time"}),
}
ALLOWED_DATA_KEYS = {
    (domain, service): frozenset()
    for domain, services in ALLOWED_SERVICES.items()
    for service in services
}
ALLOWED_DATA_KEYS.update({
    ("climate", "set_temperature"): frozenset({"temperature"}),
    ("climate", "set_hvac_mode"): frozenset({"hvac_mode"}),
    ("climate", "set_fan_mode"): frozenset({"fan_mode"}),
    ("climate", "set_swing_mode"): frozenset({"swing_mode"}),
    ("cover", "set_cover_position"): frozenset({"position"}),
    ("fan", "set_percentage"): frozenset({"percentage"}),
    ("fan", "oscillate"): frozenset({"oscillating"}),
    ("water_heater", "set_temperature"): frozenset({"temperature"}),
    ("humidifier", "set_humidity"): frozenset({"humidity"}),
    ("light", "turn_on"): frozenset({"brightness_pct", "brightness_step_pct"}),
    ("media_player", "volume_set"): frozenset({"volume_level"}),
    ("media_player", "volume_mute"): frozenset({"is_volume_muted"}),
    ("vacuum", "start"): frozenset({"darak_modes"}),
    ("vacuum", "clean_area"): frozenset({"cleaning_area_id", "darak_modes"}),
    ("vacuum", "darak_set_modes"): frozenset({"darak_modes"}),
})
ALLOWED_DATA_KEYS.update({
    ("todo", "add_item"): frozenset({"item"}),
    ("todo", "update_item"): frozenset({"item", "status"}),
    ("todo", "remove_item"): frozenset({"item"}),
})


def _valid_item(value):
    return (isinstance(value, str) and 1 <= len(value.strip()) <= 100
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value))


def bounded_response(op_id, domain, data, response, entity_ids=(), max_bytes=65536):
    key, limit = {
        "weather": ("forecast", 24 if data.get("type") == "hourly" else 8),
        "todo": ("items", 50), "calendar": ("events", 50),
    }[domain]
    result = {}
    for entity_id in entity_ids:
        value = response.get(entity_id)
        rows = value.get(key) if isinstance(value, dict) else None
        if isinstance(rows, list):
            result[entity_id] = {key: rows[:limit]}
    payload = {"type": "op_result", "id": op_id, "ok": True, "response": result}
    while len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) > max_bytes:
        lists = [value[key] for value in payload["response"].values()
                 if isinstance(value.get(key), list) and value[key]]
        if not lists:
            raise OpRejected("oversize")
        max(lists, key=len).pop()
    return payload


class OpRejected(ValueError):
    """An untrusted service message or operation failed validation."""

    def __init__(self, code="not_allowed"):
        super().__init__(code)
        self.code = code


class NoPhone(Exception):
    """A timer op came from a device that is not a Home Assistant companion phone."""


class Full(Exception):
    """The phone already has the maximum number of pending reminders."""


TIMER_KEYS = frozenset({"type", "id", "op", "action", "kind", "seconds", "text"})
MAX_TIMER_SECONDS = 86400
MAX_ALARM_SECONDS = 2764800  # 32 days, contract v2 (alarms and reminders)
MAX_REMINDER_TEXT = 100
VOICE_DEVICE_KINDS = frozenset({
    "light", "ac", "heater", "fan", "curtain", "shade", "tv", "appliance", "other",
    "vacuum", "water_heater", "humidifier", "valve", "button", "lock",
})


def _valid_reminder_text(value) -> bool:
    if not isinstance(value, str) or any(unicodedata.category(char) == "Cc" for char in value):
        return False
    return 1 <= len(value.strip()) <= MAX_REMINDER_TEXT


def _valid_timer_op(message) -> bool:
    if set(message) - TIMER_KEYS:
        return False
    kind = message.get("kind")
    if kind not in {"timer", "alarm", "reminder"}:
        return False
    action = message.get("action")
    if action == "set":
        seconds = message.get("seconds")
        limit = MAX_TIMER_SECONDS if kind == "timer" else MAX_ALARM_SECONDS
        if type(seconds) is not int or not 1 <= seconds <= limit:
            return False
        if kind == "reminder":
            return _valid_reminder_text(message.get("text"))
        return "text" not in message
    if action in {"cancel", "query"}:
        return "seconds" not in message and "text" not in message
    return False


def valid_schedule_op(message, catalog_ids, sensitive_covers=()) -> bool:
    keys = {"type", "id", "op", "action"}
    action = message.get("action")
    if action in {"query", "cancel"}:
        return set(message) == keys
    if action != "set" or set(message) != keys | {"seconds", "label", "calls"}:
        return False
    if type(message["seconds"]) is not int or not 60 <= message["seconds"] <= 86400:
        return False
    if not _valid_reminder_text(message["label"]):
        return False
    calls = message["calls"]
    if not isinstance(calls, list) or not 1 <= len(calls) <= 5:
        return False
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"domain", "service", "entity_ids", "data"}:
            return False
        domain, service = call["domain"], call["service"]
        if (isinstance(domain, str) and domain in {"lock", "valve"}
                or (domain, service) == ("button", "press")):
            return False
        try:
            validate_service_op(call, catalog_ids)
        except (OpRejected, AttributeError, TypeError):
            return False
        # Garage, gate and door covers are never scheduled, opted in or not.
        if any(entity_id in sensitive_covers for entity_id in call["entity_ids"]):
            return False
    return True


SENSITIVE_COVER_CLASSES = frozenset({"garage", "gate", "door"})


def sensitive_cover_rows(rows):
    """Catalog rows of garage, gate and door covers, which need the security opt-in."""
    return {row["entity_id"]: row for row in rows
            if row["entity_id"].startswith("cover.")
            and row.get("device_class") in SENSITIVE_COVER_CLASSES}


def _cover_needs_opt_in(service, data, row):
    # Mirrors the service: garage and gate move only when opted in; a door only when
    # opening or raising the position (closing and stopping are never the risk).
    if row["device_class"] != "door" or service == "open_cover":
        return True
    if service != "set_cover_position":
        return False
    position = data.get("position") if isinstance(data, dict) else None
    return (not isinstance(position, (int, float)) or isinstance(position, bool)
            or position > row.get("current_position", 0))


def catalog_row(
    entity_id,
    name,
    state,
    area=None,
    device_class="",
    unit="",
    current_temperature=None,
    device_id="",
    *,
    device_name="",
    aliases=(),
    current_position=None,
    hvac_modes=(),
    dimmable=None,
    brightness_pct=None,
    temperature=None,
    min_temp=None,
    max_temp=None,
    supported_features=None,
    percentage=None,
    fan_modes=(),
    swing_modes=(),
    humidity=None,
    min_humidity=None,
    max_humidity=None,
    voice=None,
    security_voice=False,
    members=(),
    clean_areas=None,
    cleaning_modes=None,
):
    valid_temperature = (
        isinstance(current_temperature, (int, float))
        and not isinstance(current_temperature, bool)
        and (not isinstance(current_temperature, float) or math.isfinite(current_temperature))
    )
    row = {
        "entity_id": str(entity_id),
        "name": str(name)[:255],
        "state": str(state)[:255],
        "area": area[:255] if isinstance(area, str) else None,
        "device_class": str(device_class or "")[:64],
        "unit": str(unit or "")[:32],
        "current_temperature": current_temperature if valid_temperature else None,
        "device_id": str(device_id or "")[:64],
    }
    if isinstance(device_name, str) and device_name.strip():
        row["device_name"] = device_name.strip()[:128]
    if isinstance(voice, dict):
        kind = voice.get("kind")
        if isinstance(kind, str) and kind in VOICE_DEVICE_KINDS:
            row["voice_kind"] = kind
            if voice.get("confirmed") is True:
                row["voice_confirmed"] = True
    if security_voice is True:
        row["security_voice"] = True
    if isinstance(members, (list, tuple)) and len(members) <= 50:
        cleaned_members = []
        for member in members:
            if not isinstance(member, dict):
                break
            values = {key: member.get(key) for key in ("entity_id", "domain", "device_class", "name")}
            if not all(isinstance(value, str) and (value or key == "device_class")
                       and value.isprintable() for key, value in values.items()):
                break
            cleaned_members.append({key: value[:limit] for key, value, limit in zip(
                values.keys(), values.values(), (255, 64, 64, 255), strict=True)})
        else:
            if members:
                row["members"] = cleaned_members
    if (isinstance(temperature, (int, float)) and not isinstance(temperature, bool)
            and (not isinstance(temperature, float) or math.isfinite(temperature))):
        row["temperature"] = temperature
    for key, value in (("min_temp", min_temp), ("max_temp", max_temp)):
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))):
            row[key] = value
    aliases = clean_aliases(aliases)
    if aliases:
        row["aliases"] = aliases
    valid_position = (
        isinstance(current_position, int) and not isinstance(current_position, bool)
    ) or (
        isinstance(current_position, float)
        and math.isfinite(current_position)
        and current_position.is_integer()
    )
    if valid_position and 0 <= current_position <= 100:
        row["current_position"] = int(current_position)
    allowed_hvac_modes = {"off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"}
    if isinstance(hvac_modes, (list, tuple, set, frozenset)):
        modes = list(dict.fromkeys(mode for mode in hvac_modes
                                   if isinstance(mode, str) and mode in allowed_hvac_modes))[:7]
        if modes:
            row["hvac_modes"] = modes
    if type(dimmable) is bool:
        row["dimmable"] = dimmable
    if isinstance(brightness_pct, int) and not isinstance(brightness_pct, bool) and 0 <= brightness_pct <= 100:
        row["brightness_pct"] = int(brightness_pct)
    if (isinstance(supported_features, int) and not isinstance(supported_features, bool)
            and 0 <= supported_features <= 2**31 - 1):
        row["supported_features"] = int(supported_features)
    if clean_areas is not None:
        row["clean_areas"] = clean_areas
    if entity_id.startswith("vacuum.") and isinstance(cleaning_modes, dict):
        row["cleaning_modes"] = cleaning_modes
    for key, value in (("percentage", percentage), ("humidity", humidity),
                       ("min_humidity", min_humidity), ("max_humidity", max_humidity)):
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
            row[key] = int(value)
    for key, values in (("fan_modes", fan_modes), ("swing_modes", swing_modes)):
        if (isinstance(values, (list, tuple)) and len(values) <= 12
                and all(isinstance(mode, str) and 1 <= len(mode) <= 32 and mode.isprintable()
                        for mode in values)
                and len(set(values)) == len(values) and values):
            row[key] = list(values)
    return row


def catalog_metadata(entry, device, attributes):
    """Resolve entity registry metadata with Home Assistant's device fallbacks."""
    attributes = attributes if isinstance(attributes, dict) else {}
    return {
        "area_id": (getattr(entry, "area_id", None) if entry else None)
                   or (getattr(device, "area_id", None) if device else None),
        "device_class": ((getattr(entry, "device_class", None)
                          or getattr(entry, "original_device_class", None))
                         or attributes.get("device_class") or ""),
        "unit": ((getattr(entry, "unit_of_measurement", None) if entry else None)
                 or attributes.get("unit_of_measurement") or ""),
    }


def _clean_alias(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 1 <= len(value) <= 80 and value.isprintable() else None


def clean_aliases(values):
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    return sorted({value for item in values if (value := _clean_alias(item))})[:12]


def catalog_area_rows(areas):
    return [{"name": name[:255], "aliases": aliases}
            for name, aliases in list(areas.items())[:500] if aliases]


def catalog_aliases(values, used_bytes=0, exhausted=False):
    aliases = clean_aliases(values)
    size = sum(len(value.encode("utf-8")) for value in aliases)
    if exhausted or used_bytes + size > 262144:
        return (), used_bytes, True
    return tuple(aliases), used_bytes + size, False


def include_entity(entity_id, device_class, exposed, hidden, voice_options=None):
    if not isinstance(entity_id, str) or len(entity_id) > 255:
        return False
    if not ENTITY_ID_RE.fullmatch(entity_id):
        return False
    domain = entity_id.partition(".")[0]
    if domain == "weather":
        return not hidden
    if domain == "sun":
        return entity_id == "sun.sun" and not hidden
    if domain == "binary_sensor":
        return exposed and not hidden and device_class in {
            "door", "window", "garage_door", "opening", "lock", "motion",
            "occupancy", "presence",
        }
    confirmed_private_device = (
        isinstance(voice_options, dict) and (
            voice_options.get("confirmed") is True
            and ((domain == "lock" and voice_options.get("kind") == "lock")
                 or (domain == "valve" and device_class != "water" and voice_options.get("kind") == "valve"))
        )
    )
    return bool(domain in CATALOG_DOMAINS and (exposed or confirmed_private_device) and not hidden
                and (domain != "sensor" or device_class in {"temperature", "humidity", "illuminance"}
                     or entity_id.endswith("_illumination")))


def parse_service_message(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise OpRejected("oversize")
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        raise OpRejected("malformed") from None
    if not isinstance(message, dict):
        raise OpRejected("malformed")
    if not isinstance(message.get("type"), str):
        raise OpRejected("malformed")
    if message["type"] not in {"op", "final", "error", "preview"}:
        raise OpRejected("malformed")
    kind = message["type"]
    if kind == "preview":
        if set(message) != {"type", "v", "heard", "engine", "reason", "source",
                            "actions", "operations", "noops", "offline", "corrections"}:
            raise OpRejected("malformed")
        if (type(message["v"]) is not int or message["v"] != 1
                or not isinstance(message["heard"], str) or len(message["heard"]) > 512
                or not isinstance(message["engine"], str) or message["engine"] not in {"v2", "legacy"}
                or not isinstance(message["reason"], str) or len(message["reason"]) > 64
                or not isinstance(message["source"], str) or message["source"] not in {"grammar", "llm", ""}):
            raise OpRejected("malformed")
        if (not isinstance(message["actions"], list) or len(message["actions"]) > 32
                or any(not isinstance(item, str) for item in message["actions"])):
            raise OpRejected("malformed")
        if not isinstance(message["operations"], list) or len(message["operations"]) > 32:
            raise OpRejected("malformed")
        for operation in message["operations"]:
            if (not isinstance(operation, dict)
                    or set(operation) not in ({"service", "entity_ids", "targets"},
                                              {"service", "entity_ids", "targets", "cleaning"})
                    or not isinstance(operation["service"], str)
                    or len(operation["service"]) > 255 or not ENTITY_ID_RE.fullmatch(operation["service"])
                    or not isinstance(operation["entity_ids"], list)
                    or len(operation["entity_ids"]) > 300
                    or any(not isinstance(entity_id, str) or len(entity_id) > 255
                           or not ENTITY_ID_RE.fullmatch(entity_id) for entity_id in operation["entity_ids"])
                    or not isinstance(operation["targets"], list)
                    or len(operation["targets"]) > 300
                    or any(not _valid_preview_target(target) for target in operation["targets"])):
                raise OpRejected("malformed")
            if "cleaning" in operation and not _valid_preview_cleaning(operation["cleaning"]):
                raise OpRejected("malformed")
        for key in ("noops", "offline"):
            if (not isinstance(message[key], list) or len(message[key]) > 300
                    or any(not _valid_preview_target(target) for target in message[key])):
                raise OpRejected("malformed")
        if (not isinstance(message["corrections"], list) or len(message["corrections"]) > 20
                or any(not isinstance(item, dict) or set(item) != {"heard", "matched"}
                       or not isinstance(item["heard"], str) or len(item["heard"]) > 512
                       or not isinstance(item["matched"], str) or len(item["matched"]) > 512
                       for item in message["corrections"])):
            raise OpRejected("malformed")
    elif kind == "op":
        if isinstance(message.get("id"), bool) or not isinstance(message.get("id"), int):
            raise OpRejected("malformed")
        if not isinstance(message.get("op"), str):
            raise OpRejected("malformed")
        if message["op"] not in {"call_service", "timer", "schedule"}:
            raise OpRejected("malformed")
        if message["op"] == "timer":
            if not _valid_timer_op(message):
                raise OpRejected("malformed")
        elif message["op"] == "schedule":
            calls = message.get("calls", [])
            ids = frozenset(entity_id for call in (calls if isinstance(calls, list) else [])
                            if isinstance(call, dict) and isinstance(call.get("entity_ids"), list)
                            for entity_id in call["entity_ids"] if isinstance(entity_id, str))
            if not valid_schedule_op(message, ids):
                raise OpRejected("malformed")
        else:
            valid_service = all(
                isinstance(message.get(key), str) for key in ("domain", "service")
            )
            valid_ids = isinstance(message.get("entity_ids"), list) and all(
                isinstance(value, str) for value in message["entity_ids"]
            )
            if not valid_service or not valid_ids or not isinstance(message.get("data"), dict):
                raise OpRejected("malformed")
            if "return_response" in message and type(message["return_response"]) is not bool:
                raise OpRejected("malformed")
    elif kind == "final":
        if "continue" in message and not isinstance(message["continue"], bool):
            raise OpRejected("malformed")
        if not isinstance(message.get("speech"), str):
            raise OpRejected("malformed")
        if not isinstance(message.get("state"), str):
            raise OpRejected("malformed")
        if message["state"] not in {"executed", "partial", "declined", "error"}:
            raise OpRejected("malformed")
        valid_executed = isinstance(message.get("executed"), list) and all(
            isinstance(value, str) for value in message["executed"]
        )
        if not valid_executed:
            raise OpRejected("malformed")
        if "conversation_id" not in message or not _optional_string(message["conversation_id"]):
            raise OpRejected("malformed")
        if "cue" in message and not isinstance(message["cue"], str):
            raise OpRejected("malformed")
    else:
        if not isinstance(message.get("code"), str) or not isinstance(message.get("speech"), str):
            raise OpRejected("malformed")
    return message


def _valid_preview_target(target):
    return (isinstance(target, dict) and set(target) == {"entity_id", "name", "area"}
            and isinstance(target["entity_id"], str) and len(target["entity_id"]) <= 255
            and ENTITY_ID_RE.fullmatch(target["entity_id"])
            and isinstance(target["name"], str) and len(target["name"]) <= 255
            and (target["area"] is None or isinstance(target["area"], str)
                 and len(target["area"]) <= 255))


_PREVIEW_AREA_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_PREVIEW_CLEANING_MODES = {
    "type": {"vacuum", "mop", "vacuum_and_mop", "smart", "smart_deep"},
    "suction": {"quiet", "normal", "strong", "max"},
    "water": {"low", "medium", "high"},
}


def _valid_preview_cleaning(cleaning):
    if not isinstance(cleaning, dict) or set(cleaning) != {"area_ids", "modes"}:
        return False
    area_ids = cleaning["area_ids"]
    modes = cleaning["modes"]
    return (isinstance(area_ids, list) and len(area_ids) <= 50
            and all(isinstance(area_id, str) and _PREVIEW_AREA_ID_RE.fullmatch(area_id)
                    for area_id in area_ids)
            and isinstance(modes, dict) and set(modes) <= set(_PREVIEW_CLEANING_MODES)
            and all(isinstance(value, str) and value in _PREVIEW_CLEANING_MODES[key]
                    for key, value in modes.items()))


def parse_verify(value, catalog_ids) -> dict | None:
    """Return a valid verification request, or ignore it."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not VERIFY_ID_RE.fullmatch(value["id"]):
        return None
    entity_ids = value.get("entity_ids")
    if (not isinstance(entity_ids, list) or not 1 <= len(entity_ids) <= 64
            or any(not isinstance(item, str) for item in entity_ids)
            or len(set(entity_ids)) != len(entity_ids)
            or not set(entity_ids) <= catalog_ids):
        return None
    after_ms = value.get("after_ms")
    if (not isinstance(after_ms, list) or not 1 <= len(after_ms) <= 2
            or any(type(item) is not int or not 0 <= item <= 30000 for item in after_ms)
            or any(a >= b for a, b in zip(after_ms, after_ms[1:]))):
        return None
    return {"id": value["id"], "entity_ids": entity_ids, "after_ms": after_ms}


def _optional_string(value):
    return value is None or isinstance(value, str)


def validate_confirmation(op, entity_ids):
    """Return a bounded confirmation request, or ignore the optional fields."""
    confirm, timeout = op.get("confirm"), op.get("confirm_ms")
    if (not isinstance(confirm, dict) or not confirm or len(confirm) > 50
            or type(timeout) is not int or not 0 <= timeout <= 5000
            or not set(confirm) <= set(entity_ids)):
        return None
    for spec in confirm.values():
        if not isinstance(spec, dict):
            return None
        if set(spec) in ({"any_of"}, {"none_of"}):
            values = next(iter(spec.values()))
            if (not isinstance(values, list) or not 1 <= len(values) <= 16
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 255
                           for value in values)):
                return None
        elif set(spec) == {"attribute", "equals"}:
            value = spec["equals"]
            scalar = (value is None or isinstance(value, str) and len(value) <= 255
                      or type(value) in (bool, int)
                      or type(value) is float and math.isfinite(value))
            if (not isinstance(spec["attribute"], str)
                    or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", spec["attribute"])
                    or not scalar):
                return None
        else:
            return None
    return confirm, timeout


def validate_service_op(op, catalog_ids: frozenset[str], *, security_ids=frozenset(),
                        sensitive_covers: Mapping[str, Mapping] | None = None,
                        clean_area_ids: Mapping[str, frozenset[str]] | None = None,
                        cleaning_modes: Mapping[str, Mapping[str, list[str]]] | None = None):
    domain, service = op.get("domain"), op.get("service")
    ids, data = op.get("entity_ids"), op.get("data")
    return_response = op.get("return_response") is True
    if "return_response" in op and type(op["return_response"]) is not bool:
        raise OpRejected()
    valid_target = (
        isinstance(domain, str)
        and isinstance(service, str)
        and ((return_response and (domain, service) in READ_SERVICES)
             or (not return_response and domain in ALLOWED_SERVICES
                 and service in ALLOWED_SERVICES.get(domain, ())))
    )
    valid_ids = (
        isinstance(ids, list)
        and 1 <= len(ids) <= 50
        and all(
            isinstance(entity_id, str)
            and entity_id in catalog_ids
            and entity_id.partition(".")[0] == domain
            for entity_id in ids
        )
    )
    if not valid_target or not valid_ids:
        raise OpRejected()
    if domain == "lock" and service == "unlock" and any(entity_id not in security_ids for entity_id in ids):
        raise OpRejected()
    if domain == "cover" and any(
            entity_id not in security_ids and entity_id in (sensitive_covers or {})
            and _cover_needs_opt_in(service, data, sensitive_covers[entity_id])
            for entity_id in ids):
        raise OpRejected()
    allowed = (READ_SERVICES.get((domain, service), frozenset()) if return_response
               else ALLOWED_DATA_KEYS.get((domain, service), frozenset()))
    if not isinstance(data, dict) or not set(data) <= allowed:
        raise OpRejected()
    if domain == "vacuum" and service in {"start", "clean_area", "darak_set_modes"}:
        has_modes = "darak_modes" in data
        if service == "darak_set_modes" and set(data) != {"darak_modes"}:
            raise OpRejected()
        if has_modes:
            requested = data["darak_modes"]
            allowed_dimensions = {"suction", "type", "water"}
            names = (cleaning_modes or {}).get(ids[0], {})
            if (len(ids) != 1 or not isinstance(requested, dict) or not requested
                    or set(requested) - allowed_dimensions
                    or any(not isinstance(name, str)
                           or name not in names.get(dimension, ())
                           for dimension, name in requested.items())):
                raise OpRejected()
        elif service == "darak_set_modes":
            raise OpRejected()
        if service == "clean_area":
            requested_areas = data.get("cleaning_area_id")
            allowed_keys = {"cleaning_area_id"} | ({"darak_modes"} if has_modes else set())
            if (len(ids) != 1 or set(data) != allowed_keys
                    or not isinstance(requested_areas, list) or not 1 <= len(requested_areas) <= 10
                    or any(not isinstance(area_id, str) or not 1 <= len(area_id) <= 64
                           for area_id in requested_areas)
                    or len(set(requested_areas)) != len(requested_areas)
                    or not set(requested_areas) <= (clean_area_ids or {}).get(ids[0], frozenset())):
                raise OpRejected()
            return domain, service, tuple(ids), dict(data)
        return domain, service, tuple(ids), dict(data)
    if return_response:
        if domain == "weather" and (not isinstance(data.get("type"), str)
                                    or data["type"] not in {"daily", "hourly"}):
            raise OpRejected()
        if domain == "todo" and (not isinstance(data.get("status"), str)
                                 or data["status"] not in {"needs_action", "completed"}):
            raise OpRejected()
        if domain == "calendar":
            if set(data) != READ_SERVICES[(domain, service)]:
                raise OpRejected()
            if any(not isinstance(data[key], str) or len(data[key]) > 64
                   for key in ("start_date_time", "end_date_time")):
                raise OpRejected()
            from datetime import datetime
            try:
                start = datetime.fromisoformat(data["start_date_time"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(data["end_date_time"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                raise OpRejected() from None
            try:
                seconds = (end - start).total_seconds()
            except TypeError:
                raise OpRejected() from None
            if not 0 < seconds <= 8 * 86400:
                raise OpRejected()
        return domain, service, tuple(ids), dict(data)
    if domain == "todo":
        if not _valid_item(data.get("item")) or service == "update_item" and data.get("status") != "completed":
            raise OpRejected()
        data = {**data, "item": data["item"].strip()}
        return domain, service, tuple(ids), dict(data)
    if (domain, service) == ("climate", "set_hvac_mode"):
        if not isinstance(data.get("hvac_mode"), str) or data["hvac_mode"] not in {
            "off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only",
        }:
            raise OpRejected()
        return domain, service, tuple(ids), dict(data)
    for key, value in data.items():
        if key in {"is_volume_muted", "oscillating"}:
            if not isinstance(value, bool):
                raise OpRejected()
            continue
        if key in {"fan_mode", "swing_mode"}:
            if not isinstance(value, str) or not 1 <= len(value) <= 32 or not value.isprintable():
                raise OpRejected()
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or isinstance(value, float) and not math.isfinite(value)
        ):
            raise OpRejected()
        if key in {"brightness_pct", "position", "percentage", "humidity"} and not 0 <= value <= 100:
            raise OpRejected()
        if key in {"percentage", "humidity"} and type(value) is not int:
            raise OpRejected()
        if key == "brightness_step_pct" and (not isinstance(value, int) or not -100 <= value <= 100 or value == 0):
            raise OpRejected()
        if key == "volume_level" and not 0 <= value <= 1:
            raise OpRejected()
        if key == "temperature":
            if domain == "water_heater":
                if not 0 <= value <= 100:
                    raise OpRejected()
            elif not 5 <= value <= 40:
                raise OpRejected()
    return domain, service, tuple(ids), dict(data)


def wav_from_pcm(pcm: bytes, sample_rate=16000, sample_width=2, channels=1) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()
