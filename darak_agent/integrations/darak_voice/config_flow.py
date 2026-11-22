"""Configuration flow for Darak Voice."""

from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DarakVoiceClient, DarakVoiceError
from .const import CONF_TOKEN, CONF_URL, DOMAIN
from .ops import TOKEN_RE


class DarakVoiceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up or reconfigure a Darak Voice service."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._show_form("user", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self._show_form("reconfigure", user_input)

    async def _show_form(self, step_id, user_input):
        errors = {}
        if user_input is not None:
            url, token = user_input.get(CONF_URL, "").strip(), user_input.get(CONF_TOKEN, "")
            parsed = urlsplit(url)
            invalid_url = (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            )
            if invalid_url:
                errors[CONF_URL] = "invalid_url"
            if not TOKEN_RE.fullmatch(token):
                errors[CONF_TOKEN] = "invalid_token"
            if not errors:
                try:
                    await DarakVoiceClient(async_get_clientsession(self.hass), url, token).whoami()
                except DarakVoiceError as err:
                    errors["base"] = err.code
                if not errors:
                    data = {CONF_URL: url.rstrip("/"), CONF_TOKEN: token}
                    if step_id == "reconfigure":
                        return self.async_update_reload_and_abort(
                            self._get_reconfigure_entry(), data=data
                        )
                    return self.async_create_entry(title="Darak Voice", data=data)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {vol.Required(CONF_URL): str, vol.Required(CONF_TOKEN): str}
            ),
            errors=errors,
        )
