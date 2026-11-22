"""Register Darak's secretless Google OAuth implementation with Home Assistant."""

import importlib
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import (
    CONF_CLIENT_ID,
    CONF_KEY,
    CONF_REDIRECT_URI,
    CONF_TOKEN_URL,
    DOMAIN,
    GOOGLE_DOMAIN,
    invalid_fields,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    invalid = invalid_fields(entry.data)
    if invalid:
        # Field names only: the key must never reach the log.
        _LOGGER.error("Darak account linking is misconfigured, invalid fields: %s", ", ".join(sorted(invalid)))
        return False

    # Both modules import Google's client libraries, which must not load in the event loop.
    auth = await hass.async_add_import_executor_job(importlib.import_module, f"{__package__}.auth")
    appcred = await hass.async_add_import_executor_job(
        importlib.import_module, "homeassistant.components.application_credentials"
    )
    google_creds = await hass.async_add_import_executor_job(
        importlib.import_module, "homeassistant.components.google.application_credentials"
    )
    impl = auth.DarakGoogleAuth(
        hass,
        DOMAIN,
        appcred.ClientCredential(entry.data[CONF_CLIENT_ID], "", "Darak"),
        appcred.AuthorizationServer(google_creds.AUTHORIZATION_SERVER.authorize_url, entry.data[CONF_TOKEN_URL]),
        redirect_uri=entry.data[CONF_REDIRECT_URI],
        darak_key=entry.data[CONF_KEY],
    )
    config_entry_oauth2_flow.async_register_implementation(hass, GOOGLE_DOMAIN, impl)
    entry.async_on_unload(lambda: _unregister(hass))

    # Google may have set up before us: failed ("Implementation not available"), or still mid-setup
    # (SETUP_IN_PROGRESS) with a provider lookup that began before we registered. NOT_LOADED entries
    # are left alone: bootstrap sets them up later and finds the registered implementation.
    for google_entry in hass.config_entries.async_entries(GOOGLE_DOMAIN):
        if google_entry.data.get("auth_implementation") == DOMAIN and google_entry.state in (
            ConfigEntryState.SETUP_ERROR,
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.SETUP_IN_PROGRESS,
        ):
            hass.async_create_task(_reload_google_entry(hass, google_entry.entry_id), f"darak_oauth reload {google_entry.entry_id}")
    return True


async def _reload_google_entry(hass: HomeAssistant, entry_id: str) -> None:
    # async_reload waits for the entry's setup lock, so an in-progress setup finishes first. If it failed,
    # HA started a reauth flow after async_reload's own abort ran and never clears it on success.
    if await hass.config_entries.async_reload(entry_id):
        for flow in hass.config_entries.flow.async_progress_by_handler(
            GOOGLE_DOMAIN, match_context={"entry_id": entry_id, "source": "reauth"}
        ):
            hass.config_entries.flow.async_abort(flow["flow_id"])


def _unregister(hass: HomeAssistant) -> None:
    # shortcut: HA has no public unregister; DATA_IMPLEMENTATIONS is the registry async_register_implementation fills.
    hass.data.get(config_entry_oauth2_flow.DATA_IMPLEMENTATIONS, {}).get(GOOGLE_DOMAIN, {}).pop(DOMAIN, None)


async def async_unload_entry(_hass: HomeAssistant, _entry: ConfigEntry) -> bool:
    return True


async def async_migrate_entry(_hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Version 1 entries hold no darak_key; the hub recreates them.
    _LOGGER.error("Darak account linking v%s entries cannot be migrated; recreate the entry", entry.version)
    return False
