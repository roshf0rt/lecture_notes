"""Use several Gemini keys as one: when a key runs out of quota, the next one takes over.

Note that the free quota belongs to a Google Cloud project, not to a key. Two keys
from the same project share one quota, so rotation only helps with keys from
different projects or accounts.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from typing import TypeVar
from zoneinfo import ZoneInfo

from .keys import is_gateway_key
from .gemini import AUTH, MODEL, OVERLOADED, QUOTA, UNREACHABLE, GeminiError
from .notes import NoteDocument
from .pipeline import Model

log = logging.getLogger(__name__)

T = TypeVar("T")
MINUTE_COOLDOWN = 60.0
BAD_KEY_COOLDOWN = 3600.0
# A key whose supplier is overloaded is skipped for a while; other keys still work.
OVERLOAD_COOLDOWN = 300.0
OVERLOAD_RETRIES = 2
OVERLOAD_DELAY = 4.0
# Free daily quotas reset at midnight Pacific time.
QUOTA_ZONE = ZoneInfo("America/Los_Angeles")


NO_SPEECH_KEY = (
    "Запись нельзя расшифровать ключом поставщика: он не передаёт звук модели, и она может выдумать текст. "
    "Добавьте ключ Google AI Studio в /keys или пришлите текст расшифровки. "
    "Ключ поставщика подходит для конспекта по готовому тексту."
)


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def seconds_to_quota_reset(now: datetime | None = None) -> float:
    now = now or datetime.now(QUOTA_ZONE)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


class KeyRing:
    """Remembers, across users and jobs, which keys are resting and which were used last."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._resting_until: dict[str, float] = {}
        self._last_used: dict[str, float] = {}

    def order(self, keys: Iterable[str]) -> list[str]:
        """Keys that are free first, least recently used first; resting keys last, soonest back first."""
        now = self._clock()

        def rank(key: str) -> tuple[int, float]:
            fingerprint = _fingerprint(key)
            until = self._resting_until.get(fingerprint, 0.0)
            if until > now:
                return 1, until
            return 0, self._last_used.get(fingerprint, 0.0)

        return sorted(dict.fromkeys(keys), key=rank)

    def resting(self, api_key: str) -> float:
        """Seconds until the key may be used again, 0 if it is free."""
        return max(0.0, self._resting_until.get(_fingerprint(api_key), 0.0) - self._clock())

    def rest(self, api_key: str, seconds: float) -> None:
        self._resting_until[_fingerprint(api_key)] = self._clock() + seconds

    def used(self, api_key: str) -> None:
        fingerprint = _fingerprint(api_key)
        self._last_used[fingerprint] = self._clock()
        self._resting_until.pop(fingerprint, None)


def _rest_for(error: GeminiError) -> float:
    if error.kind in (AUTH, MODEL):
        return BAD_KEY_COOLDOWN
    if error.kind in (OVERLOADED, UNREACHABLE):
        return OVERLOAD_COOLDOWN
    if error.daily:
        return seconds_to_quota_reset()
    return max(error.retry_after or 0.0, MINUTE_COOLDOWN)


class RotatingModel:
    """A Model that spreads calls over several keys and skips keys that hit a limit."""

    def __init__(
        self,
        keys: list[str],
        make_client: Callable[[str], Model],
        ring: KeyRing,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not keys:
            raise ValueError("RotatingModel needs at least one key")
        self._keys = keys
        self._make_client = make_client
        self._ring = ring
        self._sleep = sleep
        self._clients: dict[str, Model] = {}
        # Set once a gateway key has done the speech recognition, so the user can be told.

    def _client(self, api_key: str) -> Model:
        if api_key not in self._clients:
            self._clients[api_key] = self._make_client(api_key)
        return self._clients[api_key]

    async def _call(self, action: Callable[[Model], Awaitable[T]], keys: list[str] | None = None) -> T:
        last: GeminiError | None = None
        for api_key in keys if keys is not None else self._ring.order(self._keys):
            for attempt in range(OVERLOAD_RETRIES + 1):
                self._current_key = api_key
                try:
                    result = await action(self._client(api_key))
                except GeminiError as error:
                    last = error
                    if error.kind == OVERLOADED and attempt < OVERLOAD_RETRIES:
                        await self._sleep(OVERLOAD_DELAY * (attempt + 1))
                        continue
                    if error.kind not in (QUOTA, AUTH, MODEL, OVERLOADED, UNREACHABLE):
                        raise
                    log.info("Gemini key %s is resting: %s", _fingerprint(api_key), error.kind)
                    self._ring.rest(api_key, _rest_for(error))
                    break
                self._ring.used(api_key)
                return result
        assert last is not None
        if len(self._keys) > 1 and last.kind == QUOTA:
            raise GeminiError(
                f"Все ключи ({len(self._keys)}) сейчас упёрлись в лимит Gemini. "
                "Подождите или добавьте ключ из другого аккаунта Google.",
                QUOTA,
            ) from last
        raise last

    async def check_key(self) -> None:
        await self._call(lambda client: client.check_key())

    def _audio_keys(self) -> list[str]:
        """Keys for speech: Google's only. A reseller's gateway does not really take audio: the model
        either asks for the file or makes up a plausible transcript of a different lecture."""
        return self._ring.order([key for key in self._keys if not is_gateway_key(key)])

    async def transcribe(self, audio: bytes, filename: str) -> str:
        async def action(client: Model) -> str:
            text = await client.transcribe(audio, filename)
            return text

        keys = self._audio_keys()
        if not keys:
            raise GeminiError(NO_SPEECH_KEY)
        return await self._call(action, keys)

    async def outline(self, marked_text: str) -> list[dict]:
        return await self._call(lambda client: client.outline(marked_text))

    async def write(self, transcript: str, length_id: str, language: str = "auto", extras=(), part: str = "") -> NoteDocument:
        if part:
            return await self._call(lambda client: client.write(transcript, length_id, language, extras, part))
        return await self._call(lambda client: client.write(transcript, length_id, language, extras))
