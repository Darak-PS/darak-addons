"""HTTP and WebSocket client for the Darak Voice service."""

import asyncio
import logging
import re
from collections.abc import AsyncIterable
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, ClientWSTimeout

from .ops import (
    MAX_AUDIO_BYTES, MAX_MESSAGE_BYTES, STREAM_CHUNK_BYTES, parse_stream_message,
    stt_request_headers, stream_start_message, wav_from_pcm,
)

_LOGGER = logging.getLogger(__name__)
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,40}$")
_MAX_TTS_BYTES = 4 * 1024 * 1024


def normalize_language(language):
    """English for any en / en-XX pipeline language; Arabic otherwise, as before 1.3.0."""
    if isinstance(language, str):
        value = language.strip().lower()
        if value == "en" or value.startswith("en-"):
            return "en"
    return "ar"


class DarakVoiceError(Exception):
    """A safe error code returned by the Darak Voice client."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class DarakVoiceClient:
    """HTTP and WebSocket client for the Darak Voice service."""

    def __init__(self, session: ClientSession, url: str, token: str):
        self.session = session
        self.url = url.rstrip("/")
        self.token = token
        self.last_vocabulary_hash = None
        self.vocabulary_listener = None
        self._english_turns_supported = None
        self._warned_english_unsupported = False
        self._turn_prepare_supported = None
        self._turn_prepare_checked_at = None
        self._turn_prepare_clock = None
        self._prepared_turn = None
        self._prepared_lock = asyncio.Lock()
        self._prepare_request_task = None

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def whoami(self):
        try:
            async with self.session.get(
                self.url + "/v1/whoami",
                headers=self.headers,
                timeout=ClientTimeout(total=10),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise DarakVoiceError("invalid_auth")
                if response.status != 200:
                    raise DarakVoiceError("cannot_connect")
                payload = await response.json()
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("tenant"), str)
                    or isinstance(payload.get("protocol"), bool)
                    or payload.get("protocol") != 1
                    or not isinstance(payload.get("stt"), bool)
                    or not isinstance(payload.get("tts"), bool)
                    or payload.get("languages") != ["ar"]
                ):
                    raise DarakVoiceError("cannot_connect")
                turn_languages = payload.get("turn_languages")
                payload["supports_english_turns"] = isinstance(turn_languages, list) and "en" in turn_languages
                payload["supports_turn_prepare"] = payload.get("supports_turn_prepare") is True
                return payload
        except DarakVoiceError:
            raise
        except Exception as err:
            _LOGGER.warning("Darak Voice whoami failed code=cannot_connect")
            raise DarakVoiceError("cannot_connect") from err

    async def ensure_english_turns(self):
        # Remember only a yes: a service upgraded after this integration must be noticed
        # without restarting Home Assistant.
        if not self._english_turns_supported:
            identity = await self.whoami()
            self._english_turns_supported = identity["supports_english_turns"]
        if not self._english_turns_supported:
            if not self._warned_english_unsupported:
                _LOGGER.warning("Darak Voice English turns are unavailable")
                self._warned_english_unsupported = True
            raise DarakVoiceError("unsupported_language")

    async def ensure_turn_prepare(self):
        clock = self._turn_prepare_clock or asyncio.get_running_loop().time
        now = clock()
        if self._turn_prepare_supported is True:
            return True
        if self._turn_prepare_checked_at is None or now - self._turn_prepare_checked_at >= 600:
            try:
                self._turn_prepare_supported = (await self.whoami())["supports_turn_prepare"]
            except Exception:
                self._turn_prepare_supported = False
            self._turn_prepare_checked_at = clock()
        return self._turn_prepare_supported

    async def begin_turn_prepare(self, hass):
        try:
            if not await self.ensure_turn_prepare():
                return
        except Exception as err:
            _LOGGER.debug("Darak Voice turn prepare failed code=%s", getattr(err, "code", "failed"))
            return
        async with self._prepared_lock:
            await self._close_prepared_turn()
            slot = {"created_at": asyncio.get_running_loop().time(), "ws": None,
                    "rows": None, "areas": None, "error": None, "task": None}
            slot["task"] = asyncio.create_task(self._prepare_turn(hass, slot))
            slot["task"].add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            self._prepared_turn = slot

    def schedule_turn_prepare(self, hass):
        if self._prepare_request_task is not None and not self._prepare_request_task.done():
            self._prepare_request_task.cancel()
        task = asyncio.create_task(self.begin_turn_prepare(hass))
        self._prepare_request_task = task
        task.add_done_callback(self._prepare_request_done)

    def _prepare_request_done(self, task):
        if self._prepare_request_task is task:
            self._prepare_request_task = None
        if not task.cancelled() and task.exception():
            _LOGGER.debug("Darak Voice turn prepare failed code=failed")

    async def _prepare_turn(self, hass, slot):
        try:
            from .conversation import build_catalog

            slot["rows"], slot["areas"] = build_catalog(hass)
            slot["ws"] = await self.open_turn()
            slot["created_at"] = asyncio.get_running_loop().time()
            await slot["ws"].send_json({"type": "prepare", "v": 1,
                                        "catalog": slot["rows"], "areas": slot["areas"]})
        except asyncio.CancelledError:
            raise
        except Exception as err:
            slot["error"] = err
            _LOGGER.debug("Darak Voice turn prepare failed code=%s", getattr(err, "code", "failed"))
            ws = slot["ws"]
            if ws is not None and not getattr(ws, "closed", False):
                await ws.close()

    async def take_prepared_turn(self):
        async with self._prepared_lock:
            slot, self._prepared_turn = self._prepared_turn, None
        if slot is None:
            return None
        task, ws = slot["task"], slot["ws"]
        if (task is None or not task.done() or slot["error"] is not None or ws is None
                or getattr(ws, "closed", False)
                or asyncio.get_running_loop().time() - slot["created_at"] > 15):
            await self._close_prepared_turn(slot)
            return None
        return slot

    async def close_prepared_turn(self):
        task, self._prepare_request_task = self._prepare_request_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._prepared_lock:
            slot, self._prepared_turn = self._prepared_turn, None
        if slot:
            await self._close_prepared_turn(slot)

    async def _close_prepared_turn(self, slot=None):
        slot = slot or self._prepared_turn
        if slot is None:
            return
        task, ws = slot.get("task"), slot.get("ws")
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if ws is not None and not getattr(ws, "closed", False):
            try:
                await ws.close()
            except Exception:
                pass

    async def transcribe(self, wav: bytes, utterance_id: str | None = None, language: str = "ar",
                         *, preview: bool = False) -> str:
        if language == "en":
            await self.ensure_english_turns()
        headers = stt_request_headers(self.headers, utterance_id)
        if preview:
            headers["X-Darak-Preview"] = "1"
        if language == "en":
            headers["X-Darak-Language"] = "en"
        for attempt in range(2):
            try:
                response = await self.session.post(
                    self.url + "/v1/stt",
                    data=wav,
                    headers=headers,
                    timeout=ClientTimeout(total=30),
                    allow_redirects=False,
                )
            except ClientConnectionError as err:
                if attempt == 0:
                    continue
                raise DarakVoiceError("cannot_connect") from err
            except asyncio.TimeoutError as err:
                raise DarakVoiceError("timeout") from err
            except Exception as err:
                raise DarakVoiceError("failed") from err
            try:
                async with response:
                    if response.status != 200:
                        if response.status == 401:
                            raise DarakVoiceError("invalid_auth")
                        try:
                            error = await response.json()
                        except Exception:
                            error = {}
                        code = error.get("error") if isinstance(error, dict) else None
                        raise DarakVoiceError(
                            code
                            if isinstance(code, str) and _ERROR_CODE.fullmatch(code)
                            else f"http_{response.status}"
                        )
                    self._note_vocabulary_hash(response.headers.get("X-Darak-Vocabulary"))
                    payload = await response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                        raise DarakVoiceError("failed")
                    return payload["text"]
            except DarakVoiceError:
                raise
            except asyncio.TimeoutError as err:
                raise DarakVoiceError("timeout") from err
            except Exception as err:
                raise DarakVoiceError("failed") from err
        raise DarakVoiceError("cannot_connect")

    def _note_vocabulary_hash(self, vocabulary_hash):
        if vocabulary_hash == "none" or isinstance(vocabulary_hash, str) and re.fullmatch(r"[a-f0-9]{16,64}", vocabulary_hash):
            self.last_vocabulary_hash = vocabulary_hash
            if self.vocabulary_listener:
                try:
                    self.vocabulary_listener(vocabulary_hash)
                except Exception:
                    pass

    async def transcribe_stream(self, chunks: AsyncIterable[bytes], utterance_id=None, language="ar") -> str:
        if language == "en":
            await self.ensure_english_turns()
        parts = urlsplit(self.url)
        ws_url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                            parts.path + "/v1/stt/stream", parts.query, ""))
        ws = None
        pcm = bytearray()
        audio = chunks.__aiter__()
        receiver = None
        queue = asyncio.Queue()

        async def fallback():
            async for chunk in audio:
                if len(pcm) + len(chunk) > MAX_AUDIO_BYTES:
                    raise DarakVoiceError("audio_too_large")
                pcm.extend(chunk)
            return await self.transcribe(wav_from_pcm(pcm), utterance_id, language)

        async def receive_messages():
            try:
                while True:
                    message = await ws.receive()
                    kind = getattr(getattr(message, "type", None), "name", "")
                    if kind in {"CLOSED", "CLOSE", "CLOSING", "ERROR"}:
                        await queue.put(None)
                        return
                    if kind == "TEXT" or isinstance(message, str):
                        raw = message if isinstance(message, str) else message.data
                        await queue.put(parse_stream_message(raw))
                    else:
                        await queue.put(None)
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                await queue.put(None)

        async def next_message(timeout=None):
            try:
                if timeout is None:
                    return await queue.get()
                async with asyncio.timeout(timeout):
                    return await queue.get()
            except asyncio.TimeoutError:
                return ...

        def drain_messages():
            nonlocal failed, endpoint
            while not queue.empty():
                item = queue.get_nowait()
                if item is None:
                    failed = True
                elif item.get("type") == "endpoint":
                    endpoint = True
                elif item.get("type") in {"error", "result"}:
                    failed = True

        async def close_socket():
            if ws is not None and not getattr(ws, "closed", False):
                try:
                    await ws.close()
                except Exception:
                    pass

        try:
            try:
                async with asyncio.timeout(2):
                    ws = await self.session.ws_connect(ws_url, headers=self.headers, heartbeat=None, max_msg_size=65536)
                    await ws.send_json(stream_start_message(language, utterance_id))
                    message = parse_stream_message((await ws.receive()).data)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                code = "timeout" if isinstance(err, asyncio.TimeoutError) else getattr(err, "status", "connect")
                _LOGGER.debug("Darak Voice stream unavailable code=%s", code)
                await close_socket()
                return await fallback()
            if message is None or message.get("type") != "ready":
                code = message.get("code", "invalid_reply") if message and message.get("type") == "error" else "invalid_reply"
                _LOGGER.debug("Darak Voice stream unavailable code=%s", code)
                await close_socket()
                return await fallback()
            if message["mode"] == "off":
                _LOGGER.debug("Darak Voice stream unavailable code=off")
                await close_socket()
                return await fallback()

            receiver = asyncio.create_task(receive_messages())
            pending = bytearray()
            failed = False
            endpoint = False
            async for chunk in audio:
                if len(pcm) + len(chunk) > MAX_AUDIO_BYTES:
                    raise DarakVoiceError("audio_too_large")
                pcm.extend(chunk)
                if not failed:
                    pending.extend(chunk)
                    while len(pending) // 2 * 2 >= STREAM_CHUNK_BYTES:
                        size = min(len(pending) // 2 * 2, 65536)
                        try:
                            await ws.send_bytes(bytes(pending[:size]))
                            del pending[:size]
                        except Exception:
                            failed = True
                            break
                    await asyncio.sleep(0)
                    drain_messages()
                    if endpoint:
                        break
            await asyncio.sleep(0)
            drain_messages()
            if failed:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                receiver = None
                return await fallback()
            remainder = len(pending) // 2 * 2
            if remainder:
                try:
                    await ws.send_bytes(bytes(pending[:remainder]))
                except Exception:
                    return await fallback()
            try:
                await ws.send_json({"type": "end"})
            except Exception:
                return await fallback()
            deadline = asyncio.get_running_loop().time() + 35
            while True:
                item = await next_message(max(0, deadline - asyncio.get_running_loop().time()))
                if item is ...:
                    raise DarakVoiceError("timeout")
                if item is None:
                    return await fallback()
                if item.get("type") == "error":
                    raise DarakVoiceError(item["code"])
                if item.get("type") == "result":
                    self._note_vocabulary_hash(item["vocabulary"])
                    return item["text"]
                if item.get("type") not in {"endpoint"}:
                    return await fallback()
        finally:
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            await close_socket()

    async def synthesize(self, text: str, language: str) -> bytes:
        for attempt in range(2):
            try:
                response = await self.session.post(
                    self.url + "/v1/tts",
                    json={"text": text, "language": language},
                    headers=self.headers,
                    timeout=ClientTimeout(total=20),
                    allow_redirects=False,
                )
            except asyncio.TimeoutError as err:
                raise DarakVoiceError("timeout") from err
            except ClientConnectionError as err:
                if attempt == 0:
                    continue
                raise DarakVoiceError("cannot_connect") from err
            except Exception as err:
                raise DarakVoiceError("failed") from err
            try:
                async with response:
                    if response.status != 200:
                        if response.status == 401:
                            raise DarakVoiceError("invalid_auth")
                        try:
                            error = await response.json()
                        except (asyncio.TimeoutError, ClientConnectionError):
                            raise
                        except Exception:
                            error = {}
                        code = error.get("error") if isinstance(error, dict) else None
                        raise DarakVoiceError(
                            code if isinstance(code, str) and _ERROR_CODE.fullmatch(code)
                            else f"http_{response.status}"
                        )
                    content_type = response.headers.get("Content-Type", "")
                    if content_type.split(";", 1)[0].strip().lower() != "audio/mpeg":
                        raise DarakVoiceError("invalid_response")
                    # read(n) returns only what has arrived so far, which cut replies short
                    # whenever the audio came in more than one network chunk. Read to the end.
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > _MAX_TTS_BYTES:
                            raise DarakVoiceError("too_large")
                    data = bytes(data)
                    if not data:
                        raise DarakVoiceError("invalid_response")
                    return data
            except DarakVoiceError:
                raise
            except asyncio.TimeoutError as err:
                raise DarakVoiceError("timeout") from err
            except ClientConnectionError as err:
                if attempt == 0:
                    continue
                raise DarakVoiceError("cannot_connect") from err
            except Exception as err:
                raise DarakVoiceError("failed") from err
        raise DarakVoiceError("cannot_connect")

    async def put_vocabulary(self, payload: dict) -> dict:
        try:
            async with self.session.put(
                self.url + "/v1/stt/vocabulary",
                json=payload,
                headers={**self.headers, "Content-Type": "application/json"},
                timeout=ClientTimeout(total=20),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    if response.status == 401:
                        raise DarakVoiceError("invalid_auth")
                    if response.status == 429:
                        raise DarakVoiceError("rate_limited")
                    try:
                        error = await response.json()
                    except Exception:
                        error = {}
                    code = error.get("error") if isinstance(error, dict) else None
                    raise DarakVoiceError(
                        code if isinstance(code, str) and _ERROR_CODE.fullmatch(code)
                        else f"http_{response.status}"
                    )
                result = await response.json()
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise DarakVoiceError("failed")
                return result
        except DarakVoiceError:
            raise
        except asyncio.TimeoutError as err:
            raise DarakVoiceError("timeout") from err
        except Exception as err:
            raise DarakVoiceError("cannot_connect") from err

    async def post_verify(self, payload: dict) -> None:
        try:
            async with self.session.post(
                self.url + "/v1/verify",
                json=payload,
                headers={**self.headers, "Content-Type": "application/json"},
                timeout=ClientTimeout(total=10),
                allow_redirects=False,
            ) as response:
                if response.status == 204:
                    return
                try:
                    error = await response.json()
                except (asyncio.TimeoutError, ClientConnectionError):
                    raise
                except Exception:
                    error = {}
                code = error.get("error") if isinstance(error, dict) else None
                raise DarakVoiceError(
                    code if isinstance(code, str) and _ERROR_CODE.fullmatch(code)
                    else f"http_{response.status}"
                )
        except DarakVoiceError:
            raise
        except asyncio.TimeoutError as err:
            raise DarakVoiceError("timeout") from err
        except ClientConnectionError as err:
            raise DarakVoiceError("cannot_connect") from err
        except Exception as err:
            raise DarakVoiceError("cannot_connect") from err

    async def open_turn(self):
        parts = urlsplit(self.url)
        ws_url = urlunsplit(
            (
                "wss" if parts.scheme == "https" else "ws",
                parts.netloc,
                parts.path + "/v1/turn",
                parts.query,
                "",
            )
        )
        try:
            async with asyncio.timeout(10):
                return await self.session.ws_connect(
                    ws_url,
                    headers=self.headers,
                    heartbeat=20,
                    max_msg_size=MAX_MESSAGE_BYTES,
                    timeout=ClientWSTimeout(ws_receive=60, ws_close=10),
                )
        except DarakVoiceError:
            raise
        except asyncio.TimeoutError as err:
            raise DarakVoiceError("timeout") from err
        except Exception as err:
            raise DarakVoiceError("cannot_connect") from err
