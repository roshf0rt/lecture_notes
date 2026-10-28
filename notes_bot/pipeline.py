from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .gemini import GeminiError
from .notes import NoteDocument
from .render_pdf import render_pdf
from .render_tg import render_messages
from .settings import UserSettings
from .themes import get_theme

MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 400_000
Progress = Callable[[str], Awaitable[None]]


class Model(Protocol):
    async def transcribe(self, audio: bytes, filename: str) -> str: ...

    async def write(self, transcript: str, enabled: tuple[str, ...], length_id: str) -> NoteDocument: ...


@dataclass(frozen=True)
class Result:
    document: NoteDocument
    pdf: bytes | None = None
    messages: list[str] | None = None


class NotesPipeline:
    def __init__(self, model: Model) -> None:
        self._model = model

    async def run(
        self,
        settings: UserSettings,
        *,
        progress: Progress,
        text: str | None = None,
        audio: bytes | None = None,
        audio_name: str | None = None,
    ) -> Result:
        if audio is not None:
            await progress("transcribing")
            text = await self._model.transcribe(audio, audio_name or "audio.ogg")

        if text is None or len(text.strip()) < MIN_TEXT_CHARS:
            raise GeminiError("В материале слишком мало текста для конспекта.")
        if len(text) > MAX_TEXT_CHARS:
            raise GeminiError("Материал слишком длинный. Разбейте его на несколько частей.")

        await progress("writing")
        document = (await self._model.write(text, settings.blocks, settings.length)).only(settings.blocks)

        await progress("rendering")
        if settings.output == "pdf":
            theme = get_theme(settings.theme, settings.custom_color)
            pdf = await asyncio.to_thread(render_pdf, document, theme)
            return Result(document, pdf=pdf)
        return Result(document, messages=render_messages(document))
