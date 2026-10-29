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

from .audio import (
    AudioError,
    canonical_extension,
    duration_seconds,
    ffmpeg_available,
    find_pauses,
    plan_cuts,
    split_mp3,
    to_mp3,
)
from .gemini import AUTH, MODEL, OTHER, OVERLOADED, QUOTA, TRANSCRIBE_PROMPT, UNREACHABLE, GeminiError
from .transcript import junk_reason
from .notes import SYSTEM_PROMPT, NoteDocument, build_user_message, note_schema

log = logging.getLogger(__name__)

GATEWAY_URL = "https://api.aikeysforyou.com/v1"
MAX_OUTPUT_TOKENS = 16_000
# Recordings longer than MAX_PIECE_SECONDS are cut into the fewest equal pieces, at pauses in the speech.
MAX_PIECE_SECONDS = 15 * 60
REQUEST_TIMEOUT = 300.0
BAD_FORMAT = "Конспект получился в неверном формате. Попробуйте ещё раз."


KEY_IN_TEXT = re.compile(r"sk-cvc-[0-9A-Za-z]+")


def _reply_snippet(response: httpx.Response) -> str:
    text = KEY_IN_TEXT.sub("sk-cvc-…", response.text[:200]).strip()
    return f" Ответ поставщика ({response.status_code}): {text}" if text else ""


def _read_stream(response: httpx.Response) -> tuple[str, bool]:
    """Join the pieces of a server-sent event stream into one reply."""
    pieces: list[str] = []
    truncated = False
    for line in response.text.splitlines():
        if not line.startswith("data:") or line.strip() == "data: [DONE]":
            continue
        try:
            chunk = json.loads(line[len("data:"):])
            choice = chunk["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            pieces.append(content)
        truncated = truncated or choice.get("finish_reason") == "length"
    if not pieces and not truncated:
        raise GeminiError(f"Поставщик ключа вернул ответ, который бот не понимает.{_reply_snippet(response)}", OTHER)
    return "".join(pieces), truncated


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
    # Server-side trouble first: its text may mention the model, but the model is not the problem.
    if code >= 500:
        return GeminiError("Поставщик ключа сейчас перегружен или модель временно недоступна. Попробуйте через минуту.", OVERLOADED)
    if code == 401 or (code == 403 and "not_allowed" not in body and "not allowed" not in body):
        return GeminiError("Поставщик не принял ключ. Проверьте его в чате заказа.", AUTH)
    if code in (403, 404):
        return GeminiError("Эта модель не входит в пакет вашего ключа. Сообщите владельцу бота.", MODEL)
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
                json={"model": self._model, "messages": messages, "max_tokens": max_tokens, "temperature": 0, "stream": False},
            )
        except httpx.HTTPError as error:
            # Timeouts often have an empty message, so the type is logged too.
            log.warning("Gateway request failed: %s %r", type(error).__name__, str(error))
            # Timeouts and dropped connections: this key is skipped for a while, another key may work.
            raise GeminiError("Не получилось связаться с поставщиком ключа. Попробуйте позже.", UNREACHABLE) from error
        if response.status_code != 200:
            raise _classify(response)
        if response.text.lstrip().startswith("data:"):
            # Some replies arrive as a stream of server-sent events even when not asked for.
            return _read_stream(response)
        try:
            choice = response.json()["choices"][0]
            content = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise GeminiError(f"Поставщик ключа вернул ответ, который бот не понимает.{_reply_snippet(response)}", OTHER) from error
        if isinstance(content, list):  # some models answer with a list of parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content, choice.get("finish_reason") == "length"

    async def check_key(self) -> None:
        await self._post([{"role": "user", "content": "ping"}], max_tokens=16)

    async def transcribe(self, audio: bytes, filename: str) -> str:
        extension = canonical_extension(filename) or ".ogg"
        # Every piece goes out as MP3, the audio format the supplier takes; long recordings are cut first.
        try:
            duration = await duration_seconds(audio, extension) if ffmpeg_available() else 0.0
            if duration > MAX_PIECE_SECONDS:
                cuts = plan_cuts(duration, await find_pauses(audio, extension), MAX_PIECE_SECONDS)
                pieces = await split_mp3(audio, extension, cuts)
            elif extension == ".mp3":
                pieces = [audio]
            else:
                pieces = [await to_mp3(audio, extension)]
        except AudioError as error:
            raise GeminiError(str(error)) from error
        texts = [await self._transcribe_piece(piece) for piece in pieces]
        text = "\n\n".join(part for part in texts if part)
        if not text:
            raise GeminiError("В записи не нашлось речи.")
        reason = junk_reason(text)
        if reason:
            # The gateway did not pass the audio to the model. Another key may do better.
            raise GeminiError(f"Поставщик ключа не распознал аудио: {reason}.", MODEL)
        return text

    async def _transcribe_piece(self, data: bytes) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": TRANSCRIBE_PROMPT},
                {"type": "input_audio", "input_audio": {"data": encoded, "format": "mp3"}},
            ],
        }]
        text, truncated = await self._post(messages, MAX_OUTPUT_TOKENS)
        if truncated:
            raise GeminiError("Часть записи слишком длинная для одного ответа поставщика. Разрежьте запись на части поменьше.")
        return text.strip()

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
