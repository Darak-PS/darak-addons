"""Bounded tenant vocabulary source and Home Assistant refresh manager."""

import asyncio
import hashlib
import json
import logging
import re
from datetime import timedelta

from .ops import CATALOG_DOMAINS, include_entity

_LOGGER = logging.getLogger(__name__)
_MAX_BODY = 250000


def _clean(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= 80 or not value.isprintable():
        return None
    return value


def _aliases(values):
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    return sorted({value for item in values if (value := _clean(item))})[:12]


def build_source(areas, entities):
    """Build the exact service payload from registry-shaped plain dictionaries."""
    area_rows = {}
    for area in areas:
        if not isinstance(area, dict):
            continue
        name = _clean(area.get("name"))
        if name:
            area_rows[area.get("area_id", name)] = {
                "name": name, "aliases": _aliases(area.get("aliases", []))
            }

    output_entities = []
    entity_area_keys = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_id = entity.get("entity_id")
        if not include_entity(
            entity_id, entity.get("device_class", ""), entity.get("exposed", False),
            entity.get("hidden", False),
        ):
            continue
        entity_id = _clean(entity_id)
        name = _clean(entity.get("name"))
        if not entity_id or not name:
            continue
        area_key = entity.get("area_id")
        area = area_rows.get(area_key)
        area_name = _clean(entity.get("area")) or (area["name"] if area else None)
        device = _clean(entity.get("device"))
        output_entities.append({
            "entity_id": entity_id,
            "name": name,
            "aliases": _aliases(entity.get("aliases", [])),
            "area": area_name,
            "device": device,
        })
        entity_area_keys[entity_id] = area_key

    output_entities.sort(key=lambda row: row["entity_id"])
    output_entities = output_entities[:3000]
    referenced = {entity_area_keys[row["entity_id"]] for row in output_entities
                  if entity_area_keys.get(row["entity_id"]) in area_rows}
    included_areas = sorted(
        (row for key, row in area_rows.items() if key in referenced),
        key=lambda row: row["name"],
    )[:200]
    source = {"version": 1, "areas": included_areas, "entities": []}
    budget = _MAX_BODY - len(_canonical({**source, "source_hash": "0" * 32}))
    kept = []
    for row in output_entities:
        size = len(_canonical(row)) + bool(kept)
        if size > budget:
            break
        kept.append(row)
        budget -= size
    source["entities"] = kept
    return {**source, "source_hash": source_hash(source)}


def _canonical(source):
    return json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def source_hash(source):
    """Hash canonical source fields, excluding any supplied source_hash."""
    values = {key: value for key, value in source.items() if key != "source_hash"}
    return hashlib.sha256(_canonical(values)).hexdigest()[:32]


class VocabularyManager:
    """Push exposed registry vocabulary without blocking Home Assistant."""

    def __init__(self, hass, client):
        self.hass = hass
        self.client = client
        self._last_hash = None
        self._last_service_hash = None
        self._stopped = False
        self._lock = asyncio.Lock()
        self._debounce = None
        self._retry = None
        self._unsubs = []

    async def async_start(self):
        from homeassistant.helpers import area_registry, device_registry, entity_registry
        from homeassistant.helpers.event import async_track_time_interval
        from homeassistant.helpers.start import async_at_started
        from homeassistant.core import callback

        # callback() sets an attribute, which bound methods do not allow, so the
        # loop-safe listeners are closures.
        @callback
        def registry_changed(_event):
            self.request_refresh()

        @callback
        def at_started(_hass):
            self.hass.async_create_task(self._refresh())

        for event_type in (
            area_registry.EVENT_AREA_REGISTRY_UPDATED,
            device_registry.EVENT_DEVICE_REGISTRY_UPDATED,
            entity_registry.EVENT_ENTITY_REGISTRY_UPDATED,
        ):
            unsub = self.hass.bus.async_listen(event_type, registry_changed)
            if unsub:
                self._unsubs.append(unsub)
        unsub = async_track_time_interval(self.hass, self._periodic, timedelta(hours=1))
        if unsub:
            self._unsubs.append(unsub)
        self.client.vocabulary_listener = self.note_service_hash
        unsub = async_at_started(self.hass, at_started)
        if unsub:
            self._unsubs.append(unsub)

    async def async_stop(self):
        self._stopped = True
        timers = (self._debounce, self._retry)
        self._debounce = self._retry = None
        for cancel in timers:
            if cancel:
                try:
                    cancel()
                except Exception:
                    _LOGGER.exception("Failed to cancel vocabulary timer")
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                _LOGGER.exception("Failed to unsubscribe vocabulary listener")
        self._unsubs.clear()
        if getattr(self.client, "vocabulary_listener", None) == self.note_service_hash:
            self.client.vocabulary_listener = None

    def request_refresh(self):
        if self._stopped or self._debounce or self._retry:
            return
        from homeassistant.helpers.event import async_call_later
        self._debounce = async_call_later(self.hass, 30, self._debounced_refresh)

    def note_service_hash(self, value):
        self._last_service_hash = value
        if value != self._last_hash:
            self.request_refresh()

    async def _debounced_refresh(self, _now=None):
        self._debounce = None
        await self._refresh()

    async def _periodic(self, _now):
        await self._refresh()

    async def _refresh(self):
        if self._stopped or self._retry or self._lock.locked():
            return
        async with self._lock:
            try:
                payload = self._build_hass_source()
                if payload["source_hash"] == self._last_hash and self._last_service_hash == self._last_hash:
                    return
                await self.client.put_vocabulary(payload)
                self._last_hash = payload["source_hash"]
                self._last_service_hash = self._last_hash
            except Exception as err:
                code = getattr(err, "code", "failed")
                if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", code):
                    code = "failed"
                _LOGGER.warning("Darak Voice vocabulary push failed code=%s", code)
                if code in {"rate_limited", "cannot_connect", "timeout"}:
                    self._schedule_retry()

    def _schedule_retry(self):
        if self._stopped or self._retry:
            return
        from homeassistant.helpers.event import async_call_later
        self._retry = async_call_later(self.hass, 60, self._retry_refresh)

    async def _retry_refresh(self, _now=None):
        self._retry = None
        await self._refresh()

    def _build_hass_source(self):
        from homeassistant.components import conversation
        from homeassistant.components.homeassistant.exposed_entities import async_should_expose
        from homeassistant.helpers import area_registry, device_registry, entity_registry

        entities = entity_registry.async_get(self.hass)
        devices = device_registry.async_get(self.hass)
        areas = area_registry.async_get(self.hass)
        area_rows = [{"area_id": area.id, "name": area.name, "aliases": list(area.aliases)}
                     for area in areas.async_list_areas()]
        rows = []
        for state in self.hass.states.async_all(CATALOG_DOMAINS):
            entry = entities.async_get(state.entity_id)
            device = devices.async_get(entry.device_id) if entry and entry.device_id else None
            attrs = state.attributes
            area_id = (entry.area_id if entry else None) or (device.area_id if device else None)
            area = areas.async_get_area(area_id) if area_id else None
            rows.append({
                "entity_id": state.entity_id,
                "name": state.name or state.entity_id,
                "aliases": list(entry.aliases) if entry else [],
                "area_id": area_id,
                "area": area.name if area else None,
                "device": (device.name_by_user or device.name) if device else None,
                "device_class": attrs.get("device_class", "") or "",
                "exposed": async_should_expose(self.hass, conversation.DOMAIN, state.entity_id),
                "hidden": bool(entry and entry.hidden_by),
            })
        return build_source(area_rows, rows)
