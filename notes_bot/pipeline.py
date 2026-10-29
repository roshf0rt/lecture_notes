from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .audio import AudioError, prepare
from .blocks import ONE_MESSAGE_LENGTH_ID
from .gemini import INLINE_LIMIT_BYTES, GeminiError
from .notes import NoteDocument, document_text
from .render_pdf import render_pdf
from .transcript import junk_reason
from .render_tg import fit_one_message, render_messages
from .settings import UserSettings
from .outline import SPLIT_CHARS, Part, marked, paragraphs_of, plan
from .themes import get_theme

log = logging.getLogger(__name__)
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
    # The user asked for one note per topic (/semester), whatever the length.
    split: bool = False
    # With /semester and a subject name: every note goes to that subject's topic, without asking.
    subject: str | None = None

    @property
    def is_audio(self) -> bool:
        return self.audio is not None or self.path is not None


class Model(Protocol):
    async def check_key(self) -> None: ...

    async def transcribe(self, audio: bytes, filename: str) -> str: ...

    async def write(self, transcript: str, length_id: str, language: str = "auto", extras: tuple[str, ...] = ()) -> NoteDocument: ...

    async def outline(self, marked_text: str) -> list[dict]: ...


@dataclass(frozen=True)
class Result:
    document: NoteDocument
    transcript: str
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
        audio_mime: str | None = None,
        sources: list[Source] | None = None,
        premium: bool = False,
        on_transcript: Callable[[str], Awaitable[None]] | None = None,
    ) -> Result:
        """Make one note. Several sources are read in order and treated as parts of one lecture."""
        return (await self.run_parts(settings, progress=progress, text=text, audio=audio, audio_name=audio_name,
                                     audio_mime=audio_mime, sources=sources, premium=premium,
                                     on_transcript=on_transcript))[0]

    async def run_parts(
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
        on_transcript: Callable[[str], Awaitable[None]] | None = None,
    ) -> list[Result]:
        """One note, or one per topic when the material is too long for a single note."""
        text = await self.read(sources, text=text, audio=audio, audio_name=audio_name, audio_mime=audio_mime,
                               progress=progress, on_transcript=on_transcript, language=settings.language)
        if len(text.strip()) < MIN_TEXT_CHARS:
            raise GeminiError("В материале слишком мало текста для конспекта.")
        if len(text) > MAX_TEXT_CHARS:
            raise GeminiError("Материал слишком длинный. Разбейте его на несколько частей.")
        wants_topics = any(source.split for source in sources or [])
        if len(text) <= SPLIT_CHARS and not wants_topics:
            return [await self._note(settings, text, progress, premium)]

        await progress("outlining")
        parts = await self._outline(text)
        results = []
        for number, part in enumerate(parts, start=1):
            await progress("writing", f"{number} из {len(parts)}")
            results.append(await self._note(settings, part.text, progress, premium))
        return results

    async def _outline(self, text: str) -> list[Part]:
        blocks = paragraphs_of(text)
        try:
            raw = await self._model.outline(marked(blocks))
        except GeminiError:
            log.warning("No outline from the model; splitting the text into equal parts")
            raw = []
        return plan(raw, blocks)

    async def _note(self, settings: UserSettings, text: str, progress: Progress, premium: bool) -> Result:
        await progress("writing")
        length = ONE_MESSAGE_LENGTH_ID if settings.output == "tg_one" else settings.length
        extras = () if settings.output == "tg_one" else settings.extras  # one message has no room for them
        document = await self._model.write(text, length, settings.language, extras)
        if not document.sections and not document.main_idea:
            raise GeminiError("Модель вернула пустой конспект. Попробуйте ещё раз.")

        await progress("rendering")
        if settings.output == "tg_one":
            return Result(document, text, messages=fit_one_message(document, premium=premium))
        pdf = messages = None
        if settings.output == "both_one":
            messages = await self._short_message(document, settings.language, premium)
        if settings.output in ("pdf", "both", "both_one"):
            theme = get_theme(settings.theme, settings.custom_color)
            pdf = await asyncio.to_thread(render_pdf, document, theme)
        if settings.output in ("tg", "both"):
            messages = render_messages(document, premium=premium)
        return Result(document, text, pdf=pdf, messages=messages)

    async def _short_message(self, document: NoteDocument, language: str, premium: bool) -> list[str]:
        """The full note stays in the PDF; the chat gets a short version of it, written from the note itself."""
        try:
            short = await self._model.write(document_text(document), ONE_MESSAGE_LENGTH_ID, language, ())
            if short.sections or short.main_idea:
                return fit_one_message(short, premium=premium)
        except GeminiError:
            log.warning("Could not shorten the note; cutting the full one to a message instead")
        return fit_one_message(document, premium=premium)

    async def read(
        self,
        sources: list[Source] | None = None,
        *,
        progress: Progress,
        text: str | None = None,
        audio: bytes | None = None,
        audio_name: str | None = None,
        audio_mime: str | None = None,
        on_transcript: Callable[[str], Awaitable[None]] | None = None,
        language: str = "auto",
    ) -> str:
        """The text of all sources in order: recordings are transcribed, texts are taken as they are."""
        if sources is None:
            sources = [Source(text=text, audio=audio, name=audio_name, mime=audio_mime)]
        recordings = sum(source.is_audio for source in sources)
        texts: list[str] = []
        spoken: list[str] = []  # what the recordings said, for the raw transcript file
        done = 0
        for source in sources:
            if source.is_audio:
                done += 1
                await progress("transcribing", f"{done} из {recordings}" if recordings > 1 else "")
                try:
                    texts.append(await self._transcribe(source))
                    spoken.append(texts[-1])
                except GeminiError as error:
                    if len(sources) > 1:
                        raise GeminiError(f"Файл {sources.index(source) + 1} ({source.name or 'запись'}): {error}",
                                          error.kind, error.retry_after, error.daily) from error
                    raise
            else:
                texts.append(source.text or "")
        text = _join(texts) if len(sources) > 1 else texts[0]
        reason = junk_reason(text) if recordings else None
        if reason:
            raise GeminiError(f"Расшифровка не получилась: {reason}. Попробуйте ещё раз или добавьте ключ Google.")
        if language == "ru" and mostly_latin(text):
            raise GeminiError("Расшифровка вышла на английском, хотя в настройках выбран русский. Попробуйте ещё раз.")
        if spoken and on_transcript is not None:  # handed over before the note is written, so a failure later loses nothing
            await on_transcript(_join(spoken) if len(spoken) > 1 else spoken[0])
        return text

    async def _transcribe(self, source: Source) -> str:
        try:
            audio, name = await prepare(source.audio, source.name, source.mime, inline_limit=INLINE_LIMIT_BYTES, path=source.path)
        except AudioError as error:
            raise GeminiError(str(error)) from error
        return await self._model.transcribe(audio, name)


def mostly_latin(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    return len(letters) > 200 and sum(char.isascii() for char in letters) / len(letters) > 0.6


def _join(texts: list[str]) -> str:
    """Parts of one lecture, marked so the model sees where each recording starts."""
    return "\n\n".join(f"[Часть {number} из {len(texts)}]\n{text.strip()}" for number, text in enumerate(texts, start=1))
