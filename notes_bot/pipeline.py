from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .audio import AudioError, prepare
from .blocks import BY_ID
from .gemini import INLINE_LIMIT_BYTES, GeminiError
from .notes import NoteDocument
from .render_pdf import render_pdf
from .render_tg import render_messages
from .settings import UserSettings
from .themes import get_theme

MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 400_000
# Called with a stage name and, for several recordings, which one is being read ("2 из 3").
Progress = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class Source:
    """One piece of material: a text, or a recording with its file name and MIME type."""

    text: str | None = None
    audio: bytes | None = None
    name: str | None = None
    mime: str | None = None
    # A recording already on disk (local Bot API server), used instead of audio.
    path: Path | None = None

    @property
    def is_audio(self) -> bool:
        return self.audio is not None or self.path is not None


class Model(Protocol):
    async def check_key(self) -> None: ...

    async def transcribe(self, audio: bytes, filename: str) -> str: ...

    async def write(self, transcript: str, enabled: tuple[str, ...], length_id: str, language: str = "auto") -> NoteDocument: ...


@dataclass(frozen=True)
class Result:
    document: NoteDocument
    transcript: str
    pdf: bytes | None = None
    messages: list[str] | None = None
    # Titles of blocks the user turned on but the material did not fill.
    skipped: tuple[str, ...] = ()


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
        audio_mime: str | None = None,
        sources: list[Source] | None = None,
        premium: bool = False,
    ) -> Result:
        """Make one note. Several sources are read in order and treated as parts of one lecture."""
        if sources is None:
            sources = [Source(text=text, audio=audio, name=audio_name, mime=audio_mime)]
        recordings = sum(source.is_audio for source in sources)
        texts: list[str] = []
        done = 0
        for source in sources:
            if source.is_audio:
                done += 1
                await progress("transcribing", f"{done} из {recordings}" if recordings > 1 else "")
                try:
                    texts.append(await self._transcribe(source))
                except GeminiError as error:
                    if len(sources) > 1:
                        raise GeminiError(f"Файл {sources.index(source) + 1} ({source.name or 'запись'}): {error}",
                                          error.kind, error.retry_after, error.daily) from error
                    raise
            else:
                texts.append(source.text or "")
        text = _join(texts) if len(sources) > 1 else texts[0]

        if len(text.strip()) < MIN_TEXT_CHARS:
            raise GeminiError("В материале слишком мало текста для конспекта.")
        if len(text) > MAX_TEXT_CHARS:
            raise GeminiError("Материал слишком длинный. Разбейте его на несколько частей.")

        await progress("writing")
        written = await self._model.write(text, settings.blocks, settings.length, settings.language)
        document = written.only(settings.blocks)
        skipped = tuple(BY_ID[block].title for block in settings.blocks if not document.has_block(block))

        await progress("rendering")
        if settings.output == "pdf":
            theme = get_theme(settings.theme, settings.custom_color)
            pdf = await asyncio.to_thread(render_pdf, document, theme)
            return Result(document, text, pdf=pdf, skipped=skipped)
        return Result(document, text, messages=render_messages(document, premium=premium), skipped=skipped)

    async def _transcribe(self, source: Source) -> str:
        try:
            audio, name = await prepare(source.audio, source.name, source.mime, inline_limit=INLINE_LIMIT_BYTES, path=source.path)
        except AudioError as error:
            raise GeminiError(str(error)) from error
        return await self._model.transcribe(audio, name)


def _join(texts: list[str]) -> str:
    """Parts of one lecture, marked so the model sees where each recording starts."""
    return "\n\n".join(f"[Часть {number} из {len(texts)}]\n{text.strip()}" for number, text in enumerate(texts, start=1))
