"""Darak Voice text-to-speech provider."""

import logging
import base64
from pathlib import Path

from homeassistant.components import tts
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DarakVoiceError, normalize_language
from .const import CHIME_TEXTS, DOMAIN

_LOGGER = logging.getLogger(__name__)

# iPhone voice runs require a TTS stage even when spoken replies are disabled.
SILENT_MP3 = base64.b64decode(
    "//NAxAAAAANIAAAAAExBTUU0LjBVVVVVVVVVVVVVVVVMQU1FNC4wVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/80LEWwAAA0gA"
    "AAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/80DEpAAAA0gAAAAAVVVVVVVV"
    "VVVVVVVVVVVVVVVVVUxBTUU0LjBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zQsSjAAADSAAAAABVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVUxBTUU0LjBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVf/zQMSkAAADSAAAAABVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu"
    "MFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVV//NCxKMAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "//NAxKQAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/80LEowAAA0gA"
    "AAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FNC4wVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/80DEpAAAA0gAAAAAVVVVVVVV"
    "VVVVVVVVVVVVVVVVVUxBTUU0LjBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zQsSjAAADSAAAAABVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVUxBTUU0LjBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVf/zQMSkAAADSAAAAABVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQu"
    "MFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVV//NCxKMAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTQuMFVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "//NAxKQAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/80LEowAAA0gA"
    "AAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU="
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DarakVoiceTextToSpeech(entry)])


class DarakVoiceTextToSpeech(tts.TextToSpeechEntity):
    """Text-to-speech entity backed by Darak Voice."""

    _attr_has_entity_name = True
    _attr_translation_key = "darak_speech"
    _attr_supported_languages = ["ar", "en"]
    _attr_default_language = "ar"
    _attr_supported_options = ["voice", "confirmation", "chime_rev"]
    # HA includes merged options in the TTS cache key; this prevents old cached "تمام." audio replay.
    _attr_default_options = {"voice": "wavenet", "confirmation": "chime", "chime_rev": "2"}

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._chime_audio = None
        self._chime_load_failed = False
        self._attr_unique_id = f"{entry.entry_id}-tts"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Darak Voice",
            manufacturer="Darak",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def async_get_supported_voices(self, language: str):
        if language.startswith(("ar", "en")):
            return [tts.Voice("wavenet", "Google WaveNet"), tts.Voice("silent", "Silent")]
        return None

    async def async_get_tts_audio(self, message: str, language: str, options: dict):
        normalized = message.strip().rstrip(".!،").casefold()
        if normalized in CHIME_TEXTS and options.get("confirmation", "chime") == "chime":
            if self._chime_audio is None and not self._chime_load_failed:
                try:
                    path = Path(__file__).with_name("chime.mp3")
                    self._chime_audio = await self.hass.async_add_executor_job(path.read_bytes)
                except Exception:
                    self._chime_load_failed = True
                    _LOGGER.warning("Darak Voice confirmation chime unavailable; using speech synthesis")
            if self._chime_audio is not None:
                return "mp3", self._chime_audio
        if options.get("voice") == "silent":
            return "mp3", SILENT_MP3
        try:
            return "mp3", await self.entry.runtime_data.synthesize(
                message, normalize_language(language)
            )
        except DarakVoiceError as err:
            _LOGGER.warning("Darak Voice speech synthesis failed code=%s", err.code)
        except Exception:
            _LOGGER.warning("Darak Voice speech synthesis failed code=failed")
        return None, None
