"""Configuration flow for Darak account linking."""

import voluptuous as vol

from homeassistant import config_entries

from .const import CONF_CLIENT_ID, CONF_KEY, CONF_REDIRECT_URI, CONF_TOKEN_URL, DOMAIN, invalid_fields


class DarakOAuthConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    SINGLE_INSTANCE = True

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        errors = {}
        if user_input is not None:
            errors = invalid_fields(user_input)
            if not errors:
                return self.async_create_entry(title="Darak account linking", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(field): str for field in (
                CONF_CLIENT_ID, CONF_REDIRECT_URI, CONF_TOKEN_URL, CONF_KEY)}),
            errors=errors,
        )
