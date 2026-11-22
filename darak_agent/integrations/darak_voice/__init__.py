"""Darak Voice integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DarakVoiceClient
from .const import CONF_TOKEN, CONF_URL
from .timers import TimerManager
from .vocabulary import VocabularyManager

PLATFORMS = [Platform.STT, Platform.CONVERSATION, Platform.TTS]
_LOGGER = logging.getLogger(__name__)


def _catalog_ids(hass):
    from .conversation import build_catalog
    return frozenset(row["entity_id"] for row in build_catalog(hass)[0])


def _sensitive_covers(hass):
    from .conversation import build_catalog
    from .ops import sensitive_cover_rows
    return frozenset(sensitive_cover_rows(build_catalog(hass)[0]))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .try_phrase import async_setup_service
    from .cleaning_modes import async_setup_cleaning_modes

    async_setup_service(hass)
    async_setup_cleaning_modes(hass)
    entry.runtime_data = DarakVoiceClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN]
    )
    entry.runtime_data.timer_manager = TimerManager(
        hass, lambda: _catalog_ids(hass), lambda: _sensitive_covers(hass))
    try:
        await entry.runtime_data.timer_manager.async_start()
    except Exception:
        _LOGGER.warning("Darak Voice timers setup failed code=failed")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    manager = VocabularyManager(hass, entry.runtime_data)
    entry.runtime_data.vocabulary_manager = manager
    try:
        await manager.async_start()
    except Exception:
        _LOGGER.warning("Darak Voice vocabulary setup failed code=failed")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    manager = getattr(entry.runtime_data, "vocabulary_manager", None)
    if manager:
        await manager.async_stop()
    timers = getattr(entry.runtime_data, "timer_manager", None)
    if timers:
        await timers.async_stop()
    await entry.runtime_data.close_prepared_turn()
    return True
