from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from .audio import GEMINI_MIME, SUPPORTED_HINT, canonical_extension
from .notes import SYSTEM_PROMPT, NoteDocument, build_user_message, note_schema
from .outline import OUTLINE_PROMPT, OUTLINE_SCHEMA

log = logging.getLogger(__name__)

# The whole request (prompt plus base64-encoded audio) must stay under 20 MB.
INLINE_LIMIT_BYTES = 12 * 1024 * 1024
MAX_OUTPUT_TOKENS = 32_000
UPLOAD_POLL_SECONDS = 2
UPLOAD_POLL_TRIES = 60
TRANSCRIBE_PROMPT = (
    "Расшифруй эту запись дословно на языке речи. Не переводи ни на какой язык: если лектор говорит по-русски, "
    "пиши по-русски. Расшифровывай речь основного говорящего, лектора. "
    "Посторонние разговоры, шум, кашель и фоновые голоса пропускай. Неразборчивые места пропускай, "
    "ничего не придумывай. Верни только текст расшифровки, без пояснений, заголовков и временных меток. "
    "Если речи нет, верни пустую строку."
)
# What went wrong, so a caller can decide whether another key would help.
QUOTA, AUTH, MODEL, OVERLOADED, OTHER, UNREACHABLE = "quota", "auth", "model", "overloaded", "other", "unreachable"
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s")


class GeminiError(Exception):
    """A user-facing error from speech recognition or note writing."""

    def __init__(self, message: str, kind: str = OTHER, retry_after: float | None = None, daily: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.daily = daily


def _mime_type(filename: str) -> str:
    extension = canonical_extension(filename)
    if extension not in GEMINI_MIME:
        raise GeminiError(f"Этот формат записи не поддерживается. Подойдут {SUPPORTED_HINT}.")
    return GEMINI_MIME[extension]


def _classify(error: errors.APIError) -> GeminiError:
    message = error.message or ""
    if error.code == 429:
        details = f"{message} {error.details}"
        delay = _RETRY_DELAY.search(details)
        return GeminiError(
            "Превышен лимит запросов к Gemini. Подождите минуту или попробуйте позже: бесплатные квоты обновляются ежедневно.",
            QUOTA,
            retry_after=float(delay.group(1)) if delay else None,
            daily="PerDay" in details,
        )
    if error.code in (401, 403) or "API key" in message:
        return GeminiError("Gemini не принял ключ или у него нет доступа к модели. Проверьте ключ в Google AI Studio.", AUTH)
    if error.code == 404:
        return GeminiError("Выбранная модель Gemini сейчас недоступна. Попробуйте позже или сообщите администратору бота.", MODEL)
    if error.code == 400 and "INVALID_ARGUMENT" in f"{error.status} {error.details}":
        return GeminiError("Gemini не смог прочитать материал: похоже, запись повреждена или пустая.")
    if error.code in (500, 503, 504):
        return GeminiError("Gemini сейчас перегружен. Попробуйте через минуту.", OVERLOADED)
    return GeminiError(f"Gemini вернул ошибку {error.code}. Попробуйте ещё раз.")


@asynccontextmanager
async def _friendly_errors() -> AsyncIterator[None]:
    try:
        yield
    except errors.APIError as error:
        log.warning("Gemini API error %s: %s", error.code, error.message)
        raise _classify(error) from error


def _hit_token_limit(response: types.GenerateContentResponse) -> bool:
    candidates = response.candidates or []
    return bool(candidates) and candidates[0].finish_reason == types.FinishReason.MAX_TOKENS


class GeminiClient:
    def __init__(self, api_key: str, model: str, base_url: str | None = None) -> None:
        options = types.HttpOptions(base_url=base_url) if base_url else None
        self._client = genai.Client(api_key=api_key, http_options=options)
        self._model = model

    async def check_key(self) -> None:
        """Raise GeminiError unless the key is accepted and the model answers. Models.get is not enough: it can succeed for a model that cannot generate."""
        async with _friendly_errors():
            await self._client.aio.models.generate_content(
                model=self._model,
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=16),
            )

    async def transcribe(self, audio: bytes, filename: str) -> str:
        mime = _mime_type(filename)
        uploaded_name: str | None = None
        async with _friendly_errors():
            if len(audio) <= INLINE_LIMIT_BYTES:
                media = types.Part.from_bytes(data=audio, mime_type=mime)
            else:
                uploaded = await self._upload(audio, filename, mime)
                uploaded_name = uploaded.name
                media = types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime)
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=[types.Content(role="user", parts=[types.Part(text=TRANSCRIBE_PROMPT), media])],
                    config=types.GenerateContentConfig(temperature=0),
                )
            finally:
                if uploaded_name is not None:
                    with suppress(errors.APIError):
                        await self._client.aio.files.delete(name=uploaded_name)

        text = (response.text or "").strip()
        if not text:
            raise GeminiError("В записи не нашлось речи.")
        return text

    async def outline(self, marked_text: str) -> list[dict]:
        async with _friendly_errors():
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=marked_text,
                config=types.GenerateContentConfig(
                    system_instruction=OUTLINE_PROMPT,
                    response_mime_type="application/json",
                    response_json_schema=OUTLINE_SCHEMA,
                    temperature=0,
                ),
            )
        try:
            data = json.loads(response.text or "[]")
        except json.JSONDecodeError as error:
            raise GeminiError("Не получилось разбить материал на темы.") from error
        return data if isinstance(data, list) else []

    async def write(
        self, transcript: str, length_id: str, language: str = "auto", extras: Iterable[str] = (), part: str = "",
    ) -> NoteDocument:
        async with _friendly_errors():
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=build_user_message(transcript, length_id, language, extras, part),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_json_schema=note_schema(),
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                ),
            )

        if _hit_token_limit(response):
            raise GeminiError("Конспект не уместился в ответ модели. Выберите более короткую длину.")
        if not response.text:
            raise GeminiError("Модель не вернула конспект. Попробуйте ещё раз.")
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as error:
            raise GeminiError("Модель вернула конспект в неверном формате. Попробуйте ещё раз.") from error
        try:
            return NoteDocument.model_validate(data)
        except ValidationError as error:
            raise GeminiError("Конспект получился в неверной структуре. Попробуйте ещё раз.") from error

    async def _upload(self, audio: bytes, filename: str, mime: str) -> types.File:
        uploaded = await self._client.aio.files.upload(
            file=io.BytesIO(audio),
            config=types.UploadFileConfig(mime_type=mime, display_name=filename),
        )
        for _ in range(UPLOAD_POLL_TRIES):
            if uploaded.state != types.FileState.PROCESSING:
                break
            await asyncio.sleep(UPLOAD_POLL_SECONDS)
            uploaded = await self._client.aio.files.get(name=uploaded.name)
        if uploaded.state == types.FileState.FAILED:
            raise GeminiError("Gemini не смог обработать файл записи.")
        return uploaded
