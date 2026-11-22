"""Constants for Darak account linking."""

import re

DOMAIN = "darak_oauth"
GOOGLE_DOMAIN = "google"

CONF_CLIENT_ID = "google_client_id"
CONF_REDIRECT_URI = "redirect_uri"
CONF_TOKEN_URL = "token_url"
CONF_KEY = "darak_key"

# conf key -> (validator, config-flow error key); the contract is shared with the token proxy.
FIELD_RULES = {
    CONF_CLIENT_ID: (re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$"), "invalid_client_id"),
    CONF_REDIRECT_URI: (re.compile(r"^https://[a-z0-9.-]+/oauth/google-calendar/callback$"), "invalid_redirect_uri"),
    CONF_TOKEN_URL: (re.compile(r"^https://[a-z0-9.-]+/api/google-oauth/token$"), "invalid_token_url"),
    CONF_KEY: (re.compile(r"^dgo1\.[A-Za-z0-9_-]{43}$"), "invalid_key"),
}


def invalid_fields(data) -> dict:
    """Return {field: error key} for every required field that is missing or malformed."""
    return {
        field: error
        for field, (pattern, error) in FIELD_RULES.items()
        if not isinstance(data.get(field), str) or not pattern.fullmatch(data[field])
    }
