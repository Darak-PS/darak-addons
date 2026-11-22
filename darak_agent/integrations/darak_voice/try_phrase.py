"""Admin service for safe, non-executing phrase previews."""

import asyncio
import base64
from functools import partial
from io import BytesIO
import wave

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import SupportsResponse
from homeassistant.helpers.service import async_register_admin_service

from .api import DarakVoiceError
from .const import DOMAIN, INTEGRATION_VERSION
from .ops import OpRejected, parse_service_message

SERVICE_TRY_PHRASE = "try_phrase"


def _audio(value):
    if not isinstance(value, str) or len(value) > 341336:
        raise vol.Invalid("audio must be base64 PCM")
    try:
        pcm = base64.b64decode(value, validate=True)
    except ValueError as err:
        raise vol.Invalid("audio must be base64 PCM") from err
    if len(pcm) < 9600 or len(pcm) > 256000 or len(pcm) % 2:
        raise vol.Invalid("audio must be 0.3 to 8 seconds of PCM")
    return value


def _one_input(data):
    if ("text" in data) == ("audio" in data):
        raise vol.Invalid("provide exactly one of text or audio")
    return data


SERVICE_SCHEMA = vol.All(vol.Schema({
    vol.Optional("text"): vol.All(str, str.strip, vol.Length(min=1, max=512)),
    vol.Optional("audio"): _audio,
    vol.Optional("language", default="ar"): vol.In(("ar", "en")),
}), _one_input)


def async_setup_service(hass):
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("try_phrase_registered"):
        return
    async_register_admin_service(
        hass, DOMAIN, SERVICE_TRY_PHRASE, partial(_async_try_phrase, hass),
        schema=SERVICE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    data["try_phrase_registered"] = True


async def _async_try_phrase(hass, call):
    entry = next((entry for entry in hass.config_entries.async_entries(DOMAIN)
                  if getattr(entry, "state", None) == "loaded"
                  or getattr(getattr(entry, "state", None), "value", None) == "loaded"), None)
    if entry is None:
        raise HomeAssistantError("integration_not_loaded")
    from .conversation import build_catalog

    language = call.data.get("language", "ar")
    ws = None
    try:
        async with asyncio.timeout(30):
            text = call.data.get("text", "").strip()
            if "audio" in call.data:
                pcm = base64.b64decode(call.data["audio"], validate=True)
                stream = BytesIO()
                with wave.open(stream, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(pcm)
                try:
                    text = await entry.runtime_data.transcribe(
                        stream.getvalue(), language=language, preview=True)
                except DarakVoiceError as err:
                    raise HomeAssistantError(err.code) from None
                if not text.strip():
                    return {"heard": "", "engine": "", "reason": "no_speech", "source": "",
                            "actions": [], "operations": [], "noops": [], "offline": [],
                            "corrections": []}
            rows, areas = build_catalog(hass)
            message = {
                "type": "start", "v": 1, "text": text,
                "language": language, "conversation_id": None,
                "catalog": rows, "preview": True,
                "client": {"integration_version": INTEGRATION_VERSION},
            }
            if areas:
                message["areas"] = areas
            ws = await entry.runtime_data.open_turn()
            await ws.send_json(message)
            while True:
                frame = await ws.receive()
                if frame.type.name != "TEXT":
                    raise HomeAssistantError("unexpected_frame")
                try:
                    response = parse_service_message(frame.data)
                except OpRejected as err:
                    raise HomeAssistantError(err.code) from None
                if response["type"] == "preview":
                    return {key: value for key, value in response.items()
                            if key not in {"type", "v"}}
                if response["type"] == "error":
                    raise HomeAssistantError(response["code"])
                if response["type"] in {"op", "final"}:
                    raise HomeAssistantError("preview_violation")
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
