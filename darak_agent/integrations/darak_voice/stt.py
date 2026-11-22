"""Darak Arabic speech-to-text provider."""

import asyncio
import logging
from collections.abc import AsyncIterable

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DarakVoiceError, normalize_language
from .const import DOMAIN
from .ops import new_utterance_id, remember_utterance

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DarakVoiceSpeechToText(entry)])


class DarakVoiceSpeechToText(stt.SpeechToTextEntity):
    """Arabic speech-to-text entity backed by Darak Voice."""

    _attr_has_entity_name = True
    _attr_translation_key = "arabic_speech_to_text"

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}-stt"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Darak Voice",
            manufacturer="Darak",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self):
        return ["ar", "en"]

    @property
    def supported_formats(self):
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self):
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self):
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self):
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self):
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> stt.SpeechResult:
        utterance_id = new_utterance_id()
        try:
            self.entry.runtime_data.schedule_turn_prepare(self.hass)
        except Exception as err:
            _LOGGER.debug("Darak Voice turn prepare failed code=%s", getattr(err, "code", "failed"))
        try:
            text = await self.entry.runtime_data.transcribe_stream(
                stream, utterance_id, normalize_language(metadata.language)
            )
            remember_utterance(utterance_id, text)
            return stt.SpeechResult(text, stt.SpeechResultState.SUCCESS)
        except DarakVoiceError as err:
            if err.code != "unsupported_language":
                _LOGGER.warning("Darak Voice transcription failed code=%s", err.code)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        except Exception:
            _LOGGER.warning("Darak Voice transcription failed code=failed")
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
