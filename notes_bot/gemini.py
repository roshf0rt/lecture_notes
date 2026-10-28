from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from .notes import SYSTEM_PROMPT, NoteDocument, build_user_message, note_schema

log = logging.getLogger(__name__)

# The whole request (prompt plus base64-encoded audio) must stay under 20 MB.
INLINE_LIMIT_BYTES = 12 * 1024 * 1024
MAX_OUTPUT_TOKENS = 32_000
UPLOAD_POLL_SECONDS = 2
UPLOAD_POLL_TRIES = 60
TRANSCRIBE_PROMPT = (
    "Расшифруй эту запись дословно на языке речи. Верни только текст расшифровки, "
    "без пояснений, заголовков и временных меток. Если речи нет, верни пустую строку."
)
MIME_BY_EXTENSION = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mp3",
    ".m4a": "audio/m4a",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
}


class GeminiError(Exception):
    """A user-facing error from speech recognition or note writing."""


def is_media_name(filename: str) -> bool:
    return Path(filename).suffix.lower() in MIME_BY_EXTENSION


def _mime_type(filename: str) -> str:
    try:
        return MIME_BY_EXTENSION[Path(filename).suffix.lower()]
    except KeyError as error:
        raise GeminiError("Этот формат записи не поддерживается. Пришлите ogg, mp3, m4a, wav или mp4.") from error


def _describe(error: errors.APIError) -> str:
    if error.code == 429:
        return "Превышен лимит запросов к Gemini. Подождите минуту или попробуйте позже: бесплатные квоты обновляются ежедневно."
    if error.code in (401, 403) or "API key" in (error.message or ""):
        return "Gemini не принял ключ или у него нет доступа к модели. Проверьте ключ в Google AI Studio."
    if error.code == 404:
        return "Выбранная модель Gemini сейчас недоступна. Попробуйте позже или сообщите администратору бота."
    if error.code == 503:
        return "Gemini сейчас перегружен. Попробуйте через минуту."
    return f"Gemini вернул ошибку {error.code}. Попробуйте ещё раз."


@asynccontextmanager
async def _friendly_errors() -> AsyncIterator[None]:
    try:
        yield
    except errors.APIError as error:
        log.warning("Gemini API error %s: %s", error.code, error.message)
        raise GeminiError(_describe(error)) from error


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

    async def write(self, transcript: str, enabled: Iterable[str], length_id: str) -> NoteDocument:
        enabled = list(enabled)
        async with _friendly_errors():
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=build_user_message(transcript, enabled, length_id),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_json_schema=note_schema(enabled),
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
