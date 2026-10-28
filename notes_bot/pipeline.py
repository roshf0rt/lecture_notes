from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .llm import NotesError, NotesWriter
from .notes import NoteDocument
from .render_pdf import render_pdf
from .render_tg import render_messages
from .settings import UserSettings
from .themes import get_theme
from .transcribe import Transcriber, TranscriptionError

MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 400_000
Progress = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Result:
    document: NoteDocument
    pdf: bytes | None = None
    messages: list[str] | None = None


class NotesPipeline:
    def __init__(self, transcriber: Transcriber | None, writer: NotesWriter) -> None:
        self._transcriber = transcriber
        self._writer = writer

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
            if self._transcriber is None:
                raise TranscriptionError("Распознавание речи не настроено: нужен OPENAI_API_KEY.")
            await progress("transcribing")
            text = await self._transcriber.transcribe(audio, audio_name or "audio.ogg")

        if text is None or len(text.strip()) < MIN_TEXT_CHARS:
            raise NotesError("В материале слишком мало текста для конспекта.")
        if len(text) > MAX_TEXT_CHARS:
            raise NotesError("Материал слишком длинный. Разбейте его на несколько частей.")

        await progress("writing")
        document = (await self._writer.write(text, settings.blocks, settings.length)).only(settings.blocks)

        await progress("rendering")
        if settings.output == "pdf":
            theme = get_theme(settings.theme, settings.custom_color)
            pdf = await asyncio.to_thread(render_pdf, document, theme)
            return Result(document, pdf=pdf)
        return Result(document, messages=render_messages(document))
