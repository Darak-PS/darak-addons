"""Darak Voice Assist conversation agent."""

import asyncio
import logging
import re

import voluptuous as vol
from homeassistant.components import conversation
from homeassistant.components.vacuum import VacuumEntityFeature
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound, Unauthorized
from homeassistant.helpers import intent
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, INTEGRATION_VERSION
from .api import normalize_language
from .ops import (
    CATALOG_DOMAINS,
    MAX_CATALOG,
    Full,
    NoPhone,
    OpRejected,
    bounded_response,
    catalog_row,
    catalog_metadata,
    include_entity,
    parse_service_message,
    sensitive_cover_rows,
    validate_service_op,
    clean_aliases,
    catalog_area_rows,
    catalog_aliases,
    take_utterance_status,
    parse_verify,
    validate_confirmation,
)

_LOGGER = logging.getLogger(__name__)
_ERROR_SPEECH = "صار في مشكلة بخدمة الصوت، جرّب كمان مرة."
_VERIFY_ATTRIBUTES = (
    "brightness", "color_mode", "temperature", "current_temperature", "hvac_action",
    "current_position", "volume_level", "is_volume_muted", "source", "percentage",
)
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
VACUUM_CLEAN_AREA_FEATURE = getattr(VacuumEntityFeature, "CLEAN_AREA", 16384)
CLEAN_AREA_START_DEADLINE = 8
VACUUM_MODES_DEADLINE = 6
VACUUM_MODE_READY_WAIT = 5  # one shared budget for every select to become available


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DarakVoiceConversationEntity(entry)])


class DarakVoiceConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """Conversation agent backed by the Darak Voice service."""

    _attr_has_entity_name = True
    _attr_translation_key = "palestinian_arabic"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__()
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}-conversation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Darak Voice",
            manufacturer="Darak",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._verification_tasks = set()
        self._operation_tasks = set()

    async def async_will_remove_from_hass(self) -> None:
        for task in tuple(self._verification_tasks):
            task.cancel()
        if self._verification_tasks:
            await asyncio.gather(*self._verification_tasks, return_exceptions=True)
        await super().async_will_remove_from_hass()

    @property
    def supported_languages(self):
        return ["ar", "en"]

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        intent_response = intent.IntentResponse(language=user_input.language)
        speech = _ERROR_SPEECH
        response_error = None
        continue_conversation = False
        ws = None
        reply_tasks = set()
        op_slots = asyncio.Semaphore(8)
        send_lock = asyncio.Lock()
        utterance_id, correlation, since_stt_ms = take_utterance_status(user_input.text)
        if correlation in {"stale", "text_mismatch"}:
            _LOGGER.warning("Darak Voice utterance correlation dropped reason=%s", correlation)
        try:
            timing = {}
            if since_stt_ms is not None:
                timing["since_stt_ms"] = since_stt_ms
            phase_started = asyncio.get_running_loop().time()
            catalog = self._catalog()
            timing["catalog_ms"] = max(0, round((asyncio.get_running_loop().time() - phase_started) * 1000))
            rows, area_aliases = catalog
            catalog_ids = frozenset(row["entity_id"] for row in rows)
            prepared_slot = None
            prepared = False
            language = normalize_language(user_input.language)
            async with asyncio.timeout(60):
                if language == "en":
                    await self.entry.runtime_data.ensure_english_turns()
                prepared_slot = await self.entry.runtime_data.take_prepared_turn()
                prepared = prepared_slot is not None
                if prepared:
                    ws = prepared_slot["ws"]
                    timing["connect_ms"] = 0
                else:
                    phase_started = asyncio.get_running_loop().time()
                    ws = await self.entry.runtime_data.open_turn()
                    timing["connect_ms"] = max(0, round((asyncio.get_running_loop().time() - phase_started) * 1000))
                try:
                    client = await self._client_metadata(user_input)
                except Exception:
                    client = None
                start_message = {
                    "type": "start", "v": 1, "text": user_input.text,
                    "language": language, "conversation_id": user_input.conversation_id,
                    **({"session": chat_log.conversation_id}
                       if isinstance(chat_log.conversation_id, str)
                       and _SESSION_RE.fullmatch(chat_log.conversation_id) else {}),
                    **({"utterance_id": utterance_id} if utterance_id is not None else {}),
                    **({"client": client} if client is not None else {}),
                }
                timing["prepared"] = prepared
                if prepared:
                    previous = {row["entity_id"]: row for row in prepared_slot["rows"]}
                    current = {row["entity_id"]: row for row in rows}
                    patch = [row for row in rows if previous.get(row["entity_id"]) != row]
                    removed = sorted(previous.keys() - current.keys())
                    if len(patch) > 300:
                        start_message["catalog"] = rows
                    else:
                        if patch:
                            start_message["catalog_patch"] = patch
                        if removed:
                            start_message["catalog_removed"] = removed
                    if prepared_slot["areas"] != area_aliases:
                        start_message["areas"] = area_aliases
                else:
                    start_message["catalog"] = rows
                    if area_aliases:
                        start_message["areas"] = area_aliases
                start_message["timing"] = timing
                try:
                    await ws.send_json(start_message)
                except Exception:
                    if not prepared:
                        raise
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    prepared = False
                    timing["prepared"] = False
                    phase_started = asyncio.get_running_loop().time()
                    ws = await self.entry.runtime_data.open_turn()
                    timing["connect_ms"] = max(0, round((asyncio.get_running_loop().time() - phase_started) * 1000))
                    start_message.pop("catalog_patch", None)
                    start_message.pop("catalog_removed", None)
                    start_message["catalog"] = rows
                    if area_aliases:
                        start_message["areas"] = area_aliases
                    else:
                        start_message.pop("areas", None)
                    await ws.send_json(start_message)
                while True:
                    frame = await ws.receive()
                    received_at = asyncio.get_running_loop().time()
                    if frame.type.name != "TEXT":
                        raise ValueError("unexpected WebSocket frame")
                    message = parse_service_message(frame.data)
                    if message["type"] == "op":
                        await op_slots.acquire()

                        async def run_and_reply(op_message):
                            reply = await self._run_op(op_message, catalog_ids, user_input, rows)
                            async with send_lock:
                                await ws.send_json(reply)

                        task = asyncio.create_task(run_and_reply(message))
                        reply_tasks.add(task)
                        task.add_done_callback(lambda _task: op_slots.release())
                        continue
                    if message["type"] == "final":
                        verify = parse_verify(message.get("verify"), catalog_ids)
                        if "verify" in message and verify is None:
                            _LOGGER.debug("Darak Voice verification ignored reason=invalid_request")
                        elif verify is not None:
                            self._schedule_verification(verify, received_at)
                        speech = message["speech"]
                        continue_conversation = message.get("continue") is True
                        if not continue_conversation:
                            if message["state"] == "declined":
                                response_error = intent.IntentResponseErrorCode.NO_INTENT_MATCH
                            elif message["state"] == "error":
                                response_error = intent.IntentResponseErrorCode.FAILED_TO_HANDLE
                    else:
                        speech = message["speech"] or _ERROR_SPEECH
                        response_error = intent.IntentResponseErrorCode.FAILED_TO_HANDLE
                    break
        except Exception as err:
            if getattr(err, "code", None) != "unsupported_language":
                _LOGGER.warning("Darak Voice turn failed code=%s", getattr(err, "code", "failed"))
            response_error = intent.IntentResponseErrorCode.FAILED_TO_HANDLE
        finally:
            pending = [task for task in reply_tasks if not task.done()]
            for task in pending:
                task.cancel()
            if reply_tasks:
                await asyncio.gather(*reply_tasks, return_exceptions=True)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    _LOGGER.warning("Darak Voice turn failed code=close_failed")

        if response_error:
            intent_response.async_set_error(
                response_error, speech or _ERROR_SPEECH
            )
        else:
            intent_response.async_set_speech(speech)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=user_input.agent_id or self.entity_id,
                content=speech,
            )
        )
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=continue_conversation,
        )

    async def _client_metadata(self, user_input):
        client = {"integration_version": INTEGRATION_VERSION}
        device_id = user_input.device_id
        if isinstance(device_id, str):
            client["device_id"] = device_id[:64]
            device = dr.async_get(self.hass).devices.get(device_id)
            name = (getattr(device, "name_by_user", None) or getattr(device, "name", None)) if device else None
            if isinstance(name, str):
                client["device_name"] = name[:128]
            area_id = getattr(device, "area_id", None) if device else None
            if isinstance(area_id, str):
                try:
                    area = ar.async_get(self.hass).async_get_area(area_id)
                    area_name = getattr(area, "name", None) if area else None
                    if isinstance(area_name, str) and area_name:
                        client["area"] = area_name[:255]
                except Exception:
                    pass
        satellite_id = user_input.satellite_id
        if isinstance(satellite_id, str):
            client["satellite_id"] = satellite_id[:128]
        user_id = getattr(user_input.context, "user_id", None)
        if user_id:
            try:
                user = await self.hass.auth.async_get_user(user_id)
                if user and isinstance(user.name, str):
                    client["user_name"] = user.name[:128]
            except Exception:
                pass
        return client

    def _schedule_verification(self, verify, received_at):
        coro = self._verify(verify, received_at)
        try:
            task = self.hass.async_create_background_task(
                coro, name="darak_voice_verify"
            )
            self._verification_tasks.add(task)
            task.add_done_callback(self._verification_tasks.discard)
        except Exception:
            coro.close()
            _LOGGER.debug("Darak Voice verification failed code=failed")

    async def _verify(self, verify, received_at):
        try:
            snapshots = []
            loop = asyncio.get_running_loop()
            for after_ms in verify["after_ms"]:
                await asyncio.sleep(max(0, received_at + after_ms / 1000 - loop.time()))
                states = {}
                for entity_id in verify["entity_ids"]:
                    state = self.hass.states.get(entity_id)
                    if state is None:
                        states[entity_id] = {"state": "missing", "last_changed": None, "attributes": {}}
                        continue
                    attrs = {}
                    for key in _VERIFY_ATTRIBUTES:
                        value = state.attributes.get(key)
                        if (isinstance(value, str) and len(value) <= 255
                                or isinstance(value, (int, bool))
                                or isinstance(value, float) and value == value and abs(value) != float("inf")
                                or value is None):
                            if key in state.attributes:
                                attrs[key] = value
                    states[entity_id] = {
                        "state": state.state[:255],
                        "last_changed": state.last_changed.isoformat(),
                        "attributes": attrs,
                    }
                snapshots.append({"after_ms": after_ms, "states": states})
            await self.entry.runtime_data.post_verify({"id": verify["id"], "snapshots": snapshots})
        except Exception as err:
            code = getattr(err, "code", "failed")
            if code in {"not_found", "capture_disabled", "unknown", "http_403", "http_404"}:
                _LOGGER.debug("Darak Voice verification failed code=%s", code)
            else:
                _LOGGER.warning("Darak Voice verification failed code=%s", code)

    async def _run_op(self, message, catalog_ids, user_input, rows=()):
        """Run a validated service or nested conversation operation."""
        op_id = message["id"]
        if message["op"] == "timer":
            manager = getattr(self.entry.runtime_data, "timer_manager", None)
            if manager is None:
                return _op_error(op_id, "failed")
            try:
                body = await manager.async_handle(message, user_input.device_id)
            except NoPhone:
                return _op_error(op_id, "no_phone")
            except Full:
                return _op_error(op_id, "full")
            except Exception:
                return _op_error(op_id, "failed")
            return {"type": "op_result", "id": op_id, "ok": True, **body}
        if message["op"] == "schedule":
            manager = getattr(self.entry.runtime_data, "timer_manager", None)
            if manager is None:
                return _op_error(op_id, "failed")
            try:
                body = await manager.async_schedule(message, catalog_ids)
            except Full:
                return _op_error(op_id, "full")
            except OpRejected:
                return _op_error(op_id, "not_allowed")
            except Exception:
                return _op_error(op_id, "failed")
            return {"type": "op_result", "id": op_id, "ok": True, **body}

        try:
            security_ids = frozenset()
            if message.get("domain") == "cover" or (
                    message.get("domain") == "lock" and message.get("service") == "unlock"):
                registry = er.async_get(self.hass)
                security_ids = frozenset(entity_id for entity_id in message.get("entity_ids", ())
                    if (entity := registry.async_get(entity_id)) is not None
                    and isinstance(getattr(entity, "options", None), dict)
                    and isinstance(entity.options.get("darak_voice"), dict)
                    and entity.options["darak_voice"].get("security_voice") is True)
            domain, service, ids, data = validate_service_op(
                message, catalog_ids, security_ids=security_ids,
                sensitive_covers=sensitive_cover_rows(rows),
                clean_area_ids={row["entity_id"]: frozenset(
                    area["area_id"] for area in row.get("clean_areas", ())) for row in rows},
                cleaning_modes={row["entity_id"]: row.get("cleaning_modes", {})
                                for row in rows if row["entity_id"].startswith("vacuum.")})
            confirmation = (None if message.get("return_response") is True
                            else validate_confirmation(message, ids))
            loop = asyncio.get_running_loop()
            operation_started = loop.time()
            clean_area_entity = (ids[0] if domain == "vacuum" and service == "clean_area"
                                 and len(ids) == 1 else None)
            if clean_area_entity:
                state = self.hass.states.get(clean_area_entity)
                if state is not None and state.state in {"cleaning", "returning"}:
                    return _op_error(op_id, "vacuum_busy")
                fault = _vacuum_fault_code(state)
                if fault:
                    return _op_error(op_id, fault)
            has_modes = (domain == "vacuum"
                         and service in {"start", "clean_area", "darak_set_modes"}
                         and "darak_modes" in data)
            smart_mode_off = False
            if has_modes:
                try:
                    smart_mode_off, failure = await asyncio.wait_for(
                        self._apply_vacuum_modes(ids[0], data["darak_modes"], user_input.context),
                        timeout=VACUUM_MODES_DEADLINE)
                except asyncio.TimeoutError:
                    return _op_error(op_id, "vacuum_mode_failed")
                if failure:
                    return _op_error(op_id, "vacuum_mode_failed")
                if service == "darak_set_modes":
                    return {"type": "op_result", "id": op_id, "ok": True,
                            "smart_mode_off": bool(smart_mode_off)}
                data = {key: value for key, value in data.items() if key != "darak_modes"}
            service_wait = (max(0.5, CLEAN_AREA_START_DEADLINE - (loop.time() - operation_started))
                            if has_modes else 8)
            task = self.hass.async_create_task(
                self.hass.services.async_call(
                    domain,
                    service,
                    {"entity_id": list(ids), **data},
                    blocking=True,
                    context=user_input.context,
                    return_response=message.get("return_response", False),
                )
            )
            self._operation_tasks.add(task)
            task.add_done_callback(self._operation_done)
            done, _ = await asyncio.wait({task}, timeout=service_wait)
            if not done:
                if message.get("return_response") is True:
                    return _op_error(op_id, "timeout")
                pending = {"type": "op_result", "id": op_id, "ok": True, "pending": True}
                if has_modes:
                    pending["smart_mode_off"] = bool(smart_mode_off)
                return pending
            result = task.result()
            if clean_area_entity:
                remaining = max(0, CLEAN_AREA_START_DEADLINE -
                                (loop.time() - operation_started))
                started = await self._wait_for_vacuum_started(clean_area_entity, remaining)
                if isinstance(started, str):
                    return _op_error(op_id, started)
                result = {"type": "op_result", "id": op_id, "ok": True, "started": started}
                if has_modes:
                    result["smart_mode_off"] = bool(smart_mode_off)
                return result
            if message.get("return_response") is True:
                if not isinstance(result, dict):
                    return _op_error(op_id, "failed")
                try:
                    return bounded_response(op_id, domain, data, result, ids)
                except OpRejected:
                    return _op_error(op_id, "failed")
            if confirmation:
                expected, timeout_ms = confirmation
                remaining_ms = max(0, int((8 - (loop.time() - operation_started)) * 1000))
                not_reached = await self._wait_for_confirmation(
                    expected, min(timeout_ms, remaining_ms))
                if not_reached:
                    return {"type": "op_result", "id": op_id, "ok": True,
                            "confirmed": False, "not_reached": not_reached}
                return {"type": "op_result", "id": op_id, "ok": True, "confirmed": True}
            result = {"type": "op_result", "id": op_id, "ok": True}
            if has_modes:
                result["smart_mode_off"] = bool(smart_mode_off)
            return result
        except OpRejected:
            return _op_error(op_id, "not_allowed")
        except Unauthorized:
            return _op_error(op_id, "unauthorized")
        except ServiceNotFound:
            return _op_error(op_id, "not_found")
        except asyncio.TimeoutError:
            return _op_error(op_id, "timeout")
        except (HomeAssistantError, vol.Invalid):
            return _op_error(op_id, "failed")

    async def _apply_vacuum_modes(self, vacuum_entity_id, requested, context):
        """Apply a validated set of vacuum modes using only its registered controls."""
        from collections.abc import Mapping
        from .cleaning_modes import SMART_TYPES, detect_profile, effective_profile, is_room_select

        try:
            registry = er.async_get(self.hass)
            vacuum_entry = registry.async_get(vacuum_entity_id)
            vacuum_state = self.hass.states.get(vacuum_entity_id)
            if vacuum_entry is None or vacuum_state is None:
                return False, True
            device_id = getattr(vacuum_entry, "device_id", None)
            entries = getattr(registry, "entities", {})
            entries = entries.values() if isinstance(entries, Mapping) else ()
            select_entries = [entry for entry in entries
                              if getattr(entry, "entity_id", "").startswith("select.")
                              and device_id
                              and getattr(entry, "device_id", None) == device_id
                              and not is_room_select(getattr(entry, "platform", None),
                                                     getattr(entry, "unique_id", None),
                                                     getattr(entry, "entity_id", None))]
            selects = []
            for entry in select_entries:
                state = self.hass.states.get(entry.entity_id)
                selects.append({"entity_id": entry.entity_id, "platform": entry.platform,
                                "translation_key": entry.translation_key,
                                "state": state.state if state else None,
                                "options": state.attributes.get("options", []) if state else []})
            detected = detect_profile(vacuum_state.attributes, selects, vacuum_entity_id)
            options = getattr(vacuum_entry, "options", {})
            modes_options = options.get("darak_vacuum_modes", {}) if isinstance(options, Mapping) else {}
            override = modes_options.get("modes", {}) if isinstance(modes_options, Mapping) else {}
            profile = effective_profile(detected, override, vacuum_entity_id)
            recipes = []
            smart = requested.get("type") in SMART_TYPES
            # The robot's own smart mode decides suction and water, so those are not applied.
            for dimension in ("type",) if smart else ("type", "suction", "water"):
                name = requested.get(dimension)
                if name is not None:
                    recipe = profile[dimension][name]
                    if not recipe:
                        return False, True
                    recipes.extend(recipe)
            allowed_selects = {entry.entity_id for entry in select_entries
                               if getattr(entry, "device_id", None) == device_id}
            for assignment in [*recipes, *profile["smart_mode_off"]]:
                entity_id = assignment["entity_id"]
                if entity_id != vacuum_entity_id and entity_id not in allowed_selects:
                    return False, True

            def current_value(entity_id):
                state = self.hass.states.get(entity_id)
                if entity_id == vacuum_entity_id:
                    return state.attributes.get("fan_speed") if state else None
                return state.state if state else None

            def unavailable(entity_id):
                state = self.hass.states.get(entity_id)
                return state is None or state.state in {"unavailable", "unknown"}

            smart_changed = False
            smart_recipe = [] if smart else profile["smart_mode_off"]
            requested_unavailable = any(unavailable(item["entity_id"]) for item in recipes)
            smart_needs_change = any(current_value(item["entity_id"]) != item["option"]
                                     for item in smart_recipe)
            if smart_recipe and (smart_needs_change or requested_unavailable):
                for item in smart_recipe:
                    if current_value(item["entity_id"]) == item["option"]:
                        continue
                    await self._call_mode_assignment(vacuum_entity_id, item, context)
                smart_changed = smart_needs_change

            ready_deadline = None
            applied = []
            for item in recipes:
                entity_id = item["entity_id"]
                if current_value(entity_id) == item["option"]:
                    continue
                if unavailable(entity_id):
                    # Dreame mop humidity only appears once the cleaning mode (an earlier
                    # assignment) is a mopping one, so wait right before each call.
                    loop = asyncio.get_running_loop()
                    if ready_deadline is None:
                        ready_deadline = loop.time() + VACUUM_MODE_READY_WAIT
                    await self._wait_for_mode_condition(
                        {entity_id}, lambda: not unavailable(entity_id),
                        max(0, ready_deadline - loop.time()))
                    if unavailable(entity_id):
                        return False, True
                await self._call_mode_assignment(vacuum_entity_id, item, context)
                applied.append(item)
            targets = applied
            await self._wait_for_mode_condition(
                {item["entity_id"] for item in targets},
                lambda: all(current_value(item["entity_id"]) == item["option"] for item in targets),
                2.5)
            if any(current_value(item["entity_id"]) != item["option"] for item in targets):
                return False, True
            return smart_changed, False
        except Exception:
            _LOGGER.debug("Vacuum mode operation failed for %s", vacuum_entity_id, exc_info=True)
            return False, True

    async def _call_mode_assignment(self, vacuum_entity_id, assignment, context):
        entity_id, option = assignment["entity_id"], assignment["option"]
        if entity_id.startswith("select."):
            await self.hass.services.async_call(
                "select", "select_option", {"entity_id": entity_id, "option": option},
                blocking=True, context=context)
        else:
            await self.hass.services.async_call(
                "vacuum", "set_fan_speed", {"entity_id": vacuum_entity_id, "fan_speed": option},
                blocking=True, context=context)

    async def _wait_for_mode_condition(self, entity_ids, condition, timeout_seconds):
        if condition():
            return True
        if not entity_ids:
            return False
        changed = asyncio.Event()
        unsubscribe = async_track_state_change_event(
            self.hass, list(entity_ids), lambda _event: changed.set())
        try:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while not condition():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return condition()
                changed.clear()
            return True
        finally:
            unsubscribe()

    async def _wait_for_vacuum_started(self, entity_id, timeout_seconds):
        def status():
            state = self.hass.states.get(entity_id)
            fault = _vacuum_fault_code(state)
            if fault:
                return fault
            return state is not None and state.state == "cleaning"

        current = status()
        if current:
            return current
        changed = asyncio.Event()
        unsubscribe = async_track_state_change_event(
            self.hass, [entity_id], lambda _event: changed.set()
        )
        try:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while not (current := status()):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return status()
                changed.clear()
            return current
        finally:
            unsubscribe()

    async def _wait_for_confirmation(self, expected, timeout_ms):
        def matches(entity_id, spec):
            state = self.hass.states.get(entity_id)
            if state is None or state.state in {"unavailable", "unknown"}:
                return False
            if "any_of" in spec:
                return state.state in spec["any_of"]
            if "none_of" in spec:
                return state.state not in spec["none_of"]
            return state.attributes.get(spec["attribute"]) == spec["equals"]

        pending = set(expected)
        for entity_id in tuple(pending):
            if matches(entity_id, expected[entity_id]):
                pending.remove(entity_id)
        if not pending or timeout_ms == 0:
            return sorted(pending)

        changed = asyncio.Event()
        unsubscribe = async_track_state_change_event(
            self.hass, list(pending), lambda _event: changed.set()
        )
        try:
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
            while pending:
                for entity_id in tuple(pending):
                    if matches(entity_id, expected[entity_id]):
                        pending.remove(entity_id)
                if not pending:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(changed.wait(), remaining)
                except asyncio.TimeoutError:
                    break
                changed.clear()
            return sorted(pending)
        finally:
            unsubscribe()

    def _operation_done(self, task):
        self._operation_tasks.discard(task)
        try:
            err = task.exception()
        except asyncio.CancelledError:
            return
        if err:
            _LOGGER.debug("Darak Voice service call failed after response: %s", type(err).__name__)

    def _catalog(self):
        return build_catalog(self.hass)


def build_catalog(hass):
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    rows = []
    area_aliases = {}
    alias_bytes = 0
    alias_budget_exceeded = False
    device_domains = {"climate", "cover", "fan", "input_boolean", "light",
                      "media_player", "scene", "sensor", "switch"}
    states = sorted(hass.states.async_all(CATALOG_DOMAINS),
                    key=lambda state: state.entity_id.partition(".")[0] not in device_domains)
    for state in states:
        entity_id = state.entity_id
        entry = entities.async_get(entity_id)
        device = devices.async_get(entry.device_id) if entry and entry.device_id else None
        attrs = state.attributes
        metadata = catalog_metadata(entry, device, attrs)
        device_class = metadata["device_class"]
        exposed = async_should_expose(hass, conversation.DOMAIN, entity_id)
        hidden = bool(entry and entry.hidden_by)
        voice_options = (entry.options.get("darak_voice")
                         if entry and isinstance(getattr(entry, "options", None), dict) else None)
        if not include_entity(entity_id, device_class, exposed, hidden, voice_options):
            continue
        area_id = metadata["area_id"]
        area_entry = areas.async_get_area(area_id) if area_id else None
        area = area_entry.name if area_entry else None
        aliases = entry.aliases if entry else ()
        cleaned_aliases, alias_bytes, alias_budget_exceeded = catalog_aliases(
            aliases, alias_bytes, alias_budget_exceeded
        )
        if area_entry and area and area not in area_aliases and len(area_aliases) < 500:
            cleaned_area_aliases = clean_aliases(getattr(area_entry, "aliases", ()))
            if cleaned_area_aliases:
                area_aliases[area] = cleaned_area_aliases
        row_state = state.state
        current_temperature = attrs.get("current_temperature")
        if entity_id.startswith("weather.") and current_temperature is None:
            current_temperature = attrs.get("temperature")
        if entity_id.startswith("todo."):
            items = attrs.get("todo_items")
            if isinstance(items, list):
                row_state = str(sum(1 for item in items if isinstance(item, dict)
                                    and item.get("status") == "needs_action"))
        row = catalog_row(
                entity_id, state.name or entity_id, row_state, area, device_class,
                metadata["unit"], current_temperature,
                entry.device_id if entry and entry.device_id else "",
                device_name=(getattr(device, "name_by_user", None)
                             or getattr(device, "name", None)) if device else "",
                aliases=cleaned_aliases, current_position=attrs.get("current_position"),
                hvac_modes=attrs.get("hvac_modes", ()),
                dimmable=(set(attrs.get("supported_color_modes") or ()) != {"onoff"}
                          if entity_id.startswith("light.") else None),
                brightness_pct=round(attrs["brightness"] * 100 / 255)
                if entity_id.startswith("light.") and isinstance(attrs.get("brightness"), int)
                and not isinstance(attrs.get("brightness"), bool) else None,
                temperature=attrs.get("temperature"), min_temp=attrs.get("min_temp"),
                max_temp=attrs.get("max_temp"),
                supported_features=attrs.get("supported_features"),
                clean_areas=_clean_areas(entry, attrs, areas) if entity_id.startswith("vacuum.") else None,
                cleaning_modes=_cleaning_modes(hass, entity_id, entry, attrs, entities)
                if entity_id.startswith("vacuum.") else None,
                percentage=attrs.get("percentage"),
                fan_modes=attrs.get("fan_modes", ()), swing_modes=attrs.get("swing_modes", ()),
                humidity=attrs.get("humidity"), min_humidity=attrs.get("min_humidity"),
                max_humidity=attrs.get("max_humidity"),
                voice=(entry.options.get("darak_voice")
                       if entry and isinstance(getattr(entry, "options", None), dict) else None),
                security_voice=bool(isinstance(voice_options, dict)
                                    and voice_options.get("security_voice") is True),
                members=_group_members(hass, attrs, entities, devices),
            )
        if entity_id == "sun.sun":
            for key in ("next_rising", "next_setting"):
                value = attrs.get(key)
                if isinstance(value, str) and len(value) <= 64:
                    row[key] = value
        rows.append(row)
        if len(rows) == MAX_CATALOG:
            break
    vacuum_area_ids = {area["area_id"] for row in rows for area in row.get("clean_areas", ())}
    for area_id in vacuum_area_ids:
        area_entry = areas.async_get_area(area_id)
        if area_entry and isinstance(area_entry.name, str) and area_entry.name not in area_aliases and len(area_aliases) < 500:
            cleaned_area_aliases = clean_aliases(getattr(area_entry, "aliases", ()))
            if cleaned_area_aliases:
                area_aliases[area_entry.name] = cleaned_area_aliases
    return rows, catalog_area_rows(area_aliases)


def _clean_areas(entry, attrs, area_registry):
    features = attrs.get("supported_features")
    if not isinstance(features, int) or isinstance(features, bool) or not features & VACUUM_CLEAN_AREA_FEATURE:
        return None
    options = entry.options if entry and isinstance(getattr(entry, "options", None), dict) else {}
    vacuum_options = options.get("vacuum")
    mapping = vacuum_options.get("area_mapping") if isinstance(vacuum_options, dict) else None
    if not isinstance(mapping, dict):
        return []
    areas = getattr(area_registry, "async_list_areas", lambda: [])()
    result = []
    for area in areas:
        segments = mapping.get(area.id)
        if (isinstance(area.id, str) and isinstance(area.name, str)
                and isinstance(segments, list) and segments
                and all((isinstance(segment, str) and 1 <= len(segment) <= 64)
                        or (type(segment) is int and segment >= 0) for segment in segments)):
            result.append({"area_id": area.id, "name": area.name})
            if len(result) == 50:
                break
    return result


def _cleaning_modes(hass, vacuum_entity_id, vacuum_entry, attrs, entity_registry):
    from collections.abc import Mapping
    from .cleaning_modes import (catalog_names, detect_profile, effective_profile, is_room_select,
                                 validate_override)

    try:
        device_id = getattr(vacuum_entry, "device_id", None) if vacuum_entry else None
        if not device_id:
            select_entries = []
        else:
            entries_for_device = getattr(er, "async_entries_for_device", None)
            if callable(entries_for_device):
                select_entries = entries_for_device(entity_registry, device_id)
            else:
                registry_entries = getattr(entity_registry, "entities", {})
                values = registry_entries.values() if isinstance(registry_entries, Mapping) else ()
                select_entries = [entry for entry in values
                                  if getattr(entry, "device_id", None) == device_id]
        selects = []
        for entry in select_entries:
            entity_id = getattr(entry, "entity_id", "")
            if not isinstance(entity_id, str) or not entity_id.startswith("select."):
                continue
            if is_room_select(getattr(entry, "platform", None),
                              getattr(entry, "unique_id", None), entity_id):
                continue
            state = hass.states.get(entity_id)
            selects.append({"entity_id": entity_id, "platform": getattr(entry, "platform", None),
                            "translation_key": getattr(entry, "translation_key", None),
                            "state": state.state if state else None,
                            "options": state.attributes.get("options", []) if state else []})
        detected = detect_profile(attrs, selects, vacuum_entity_id)
        options = getattr(vacuum_entry, "options", {}) if vacuum_entry else {}
        mode_options = options.get("darak_vacuum_modes", {}) if isinstance(options, Mapping) else {}
        override = mode_options.get("modes", {}) if isinstance(mode_options, Mapping) else {}
        if not validate_override(override):
            override = {}
        return catalog_names(effective_profile(detected, override, vacuum_entity_id))
    except Exception:
        _LOGGER.debug("Unable to detect cleaning modes for %s", vacuum_entity_id, exc_info=True)
        return {"suction": [], "type": [], "water": []}


def _group_members(hass, attrs, entities, devices):
    """Expose bounded metadata for group-helper risk classification."""
    member_ids = attrs.get("entity_id") if hasattr(attrs, "get") else None
    if not isinstance(member_ids, (list, tuple)):
        return ()
    found = []
    for member_id in member_ids[:50]:
        if not isinstance(member_id, str) or "." not in member_id:
            continue
        state = hass.states.get(member_id)
        member_entry = entities.async_get(member_id)
        member_device = (devices.async_get(member_entry.device_id)
                         if member_entry and member_entry.device_id else None)
        member_attrs = state.attributes if state is not None else {}
        metadata = catalog_metadata(member_entry, member_device, member_attrs)
        found.append({
            "entity_id": member_id,
            "domain": member_id.partition(".")[0],
            "device_class": str(metadata["device_class"] or ""),
            "name": str(state.name if state is not None and state.name else member_id),
        })
    return found


def _op_error(op_id, error):
    return {"type": "op_result", "id": op_id, "ok": False, "error": error}


def _vacuum_fault_code(state):
    if state is None or not (state.state == "error" or
                             getattr(state, "attributes", {}).get("has_error") is True):
        return None
    error = getattr(state, "attributes", {}).get("error", "")
    return "vacuum_no_water" if isinstance(error, str) and "water" in error.casefold() else "vacuum_fault"
