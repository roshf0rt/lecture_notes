"""Client for keys from a resold OpenAI-compatible gateway (api.aikeysforyou.com).

Used only for keys with the "sk-cvc-" prefix, which the owner adds knowingly. The
gateway is a third party: it sees the recordings and texts, and it publishes no terms
about storing them. The bot warns about that whenever such a key is added.
"""
from __future__ import annotations

import base64
import json
import logging
import re

import httpx

from .audio import canonical_extension, to_mp3
from .gemini import AUTH, MODEL, OTHER, OVERLOADED, QUOTA, TRANSCRIBE_PROMPT, GeminiError
from .notes import SYSTEM_PROMPT, NoteDocument, build_user_message, note_schema

log = logging.getLogger(__name__)

GATEWAY_URL = "https://api.aikeysforyou.com/v1"
MAX_OUTPUT_TOKENS = 16_000
REQUEST_TIMEOUT = 300.0
BAD_FORMAT = "Конспект получился в неверном формате. Попробуйте ещё раз."


KEY_IN_TEXT = re.compile(r"sk-cvc-[0-9A-Za-z]+")


def _supplier_says(response: httpx.Response) -> str:
    """The supplier's own words, so a rejected key shows why. Any key-looking text is hidden."""
    text = KEY_IN_TEXT.sub("sk-cvc-…", response.text[:200]).strip()
    return f" Ответ поставщика ({response.status_code}): {text}" if text else ""


def _classify(response: httpx.Response) -> GeminiError:
    error = _classify_kind(response)
    return GeminiError(f"{error}{_supplier_says(response)}", error.kind, error.retry_after, error.daily)


def _classify_kind(response: httpx.Response) -> GeminiError:
    code = response.status_code
    body = response.text[:300].lower()
    if code == 429:
        retry = response.headers.get("retry-after", "")
        return GeminiError(
            "Превышен лимит запросов у поставщика ключа. Подождите минуту и попробуйте снова.",
            QUOTA,
            retry_after=float(retry) if retry.replace(".", "", 1).isdigit() else None,
        )
    if code == 402:
        return GeminiError("На балансе ключа у поставщика закончились деньги. Пополните его или добавьте другой ключ.", AUTH)
    if code == 401 or (code == 403 and "model" not in body):
        return GeminiError("Поставщик не принял ключ. Проверьте его в чате заказа.", AUTH)
    if code in (403, 404) or "model" in body:
        return GeminiError("Эта модель не входит в пакет вашего ключа. Сообщите владельцу бота.", MODEL)
    if code >= 500:
        return GeminiError("Поставщик ключа сейчас перегружен. Попробуйте через минуту.", OVERLOADED)
    return GeminiError(f"Поставщик ключа вернул ошибку {code}. Попробуйте ещё раз.")


def _strip_fences(text: str) -> str:
    """Models often wrap JSON in a ```json fence even when asked not to."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


class GatewayClient:
    """The same three calls as GeminiClient, so the pipeline and key rotation do not care which one runs."""

    def __init__(self, api_key: str, model: str, base_url: str = GATEWAY_URL, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, transport=transport)

    async def _post(self, messages: list[dict], max_tokens: int) -> tuple[str, bool]:
        """Returns the reply text and whether it was cut off by the length limit."""
        try:
            response = await self._http.post(
                f"{self._url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "messages": messages, "max_tokens": max_tokens, "temperature": 0},
            )
        except httpx.HTTPError as error:
            log.warning("Gateway request failed: %s", error)
            raise GeminiError("Не получилось связаться с поставщиком ключа. Попробуйте позже.", OTHER) from error
        if response.status_code != 200:
            raise _classify(response)
        try:
            choice = response.json()["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise GeminiError("Поставщик ключа вернул ответ, который бот не понимает.", OTHER) from error
        return text, choice.get("finish_reason") == "length"

    async def check_key(self) -> None:
        await self._post([{"role": "user", "content": "ping"}], max_tokens=16)

    async def transcribe(self, audio: bytes, filename: str) -> str:
        extension = canonical_extension(filename) or ".ogg"
        if extension in (".mp3", ".wav"):
            data, audio_format = audio, extension[1:]
        else:
            # The gateway takes only WAV and MP3 audio input, so other formats are converted first.
            data, audio_format = await to_mp3(audio, extension), "mp3"
        encoded = base64.b64encode(data).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": TRANSCRIBE_PROMPT},
                {"type": "input_audio", "input_audio": {"data": encoded, "format": audio_format}},
            ],
        }]
        text, truncated = await self._post(messages, MAX_OUTPUT_TOKENS)
        if truncated:
            raise GeminiError("Запись слишком длинная для одного ответа поставщика. Разрежьте её на части.")
        text = text.strip()
        if not text:
            raise GeminiError("В записи не нашлось речи.")
        return text

    async def write(self, transcript: str, enabled, length_id: str, language: str = "auto") -> NoteDocument:
        enabled = list(enabled)
        schema = json.dumps(note_schema(enabled), ensure_ascii=False)
        messages = [
            {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nОтвет — только один JSON-объект по этой схеме, без пояснений и без обрамления в ```:\n{schema}"},
            {"role": "user", "content": build_user_message(transcript, enabled, length_id, language)},
        ]
        text, truncated = await self._post(messages, MAX_OUTPUT_TOKENS)
        if truncated:
            raise GeminiError("Конспект не уместился в ответ модели. Выберите более короткую длину.")
        if not text.strip():
            raise GeminiError("Модель не вернула конспект. Попробуйте ещё раз.")
        try:
            data = json.loads(_strip_fences(text))
        except json.JSONDecodeError as error:
            raise GeminiError("Модель вернула конспект в неверном формате. Попробуйте ещё раз.") from error
        try:
            return NoteDocument.model_validate(data)
        except ValueError as error:  # pydantic's ValidationError is a ValueError
            raise GeminiError(BAD_FORMAT) from error

    async def close(self) -> None:
        await self._http.aclose()
