"""Google OAuth implementation that never holds a client secret."""

from homeassistant.components.google.api import GoogleHybridAuth


class DarakGoogleAuth(GoogleHybridAuth):
    """Send every token request to Darak's proxy, authenticated by the per-home key."""

    def __init__(self, hass, auth_domain, credential, authorization_server, *, redirect_uri, darak_key):
        super().__init__(hass, auth_domain, credential, authorization_server)
        self._redirect_uri = redirect_uri
        self._darak_key = darak_key

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    async def _token_request(self, data: dict) -> dict:
        # The base class adds client_id and omits client_secret because ours is empty.
        data["darak_key"] = self._darak_key
        return await super()._token_request(data)
