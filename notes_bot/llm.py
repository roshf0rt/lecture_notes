from __future__ import annotations

from collections.abc import Iterable

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from .notes import SYSTEM_PROMPT, TOOL_NAME, NoteDocument, build_user_message, notes_tool


class NotesError(Exception):
    """A user-facing error while writing the notes."""


class NotesWriter:
    def __init__(self, api_key: str, model: str, max_tokens: int = 16_000) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    async def write(self, transcript: str, enabled: Iterable[str], length_id: str) -> NoteDocument:
        enabled = list(enabled)
        tool = notes_tool(enabled)
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            tools=[tool],
            # Forcing the tool makes the reply a structured object instead of free text.
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": build_user_message(transcript, enabled, length_id)}],
        )
        if response.stop_reason == "max_tokens":
            raise NotesError("Конспект не уместился в ответ модели. Выберите более короткую длину.")
        for block in response.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                try:
                    return NoteDocument.model_validate(block.input)
                except ValidationError as error:
                    raise NotesError("Конспект получился в неверной структуре. Попробуйте ещё раз.") from error
        raise NotesError("Модель не вернула конспект. Попробуйте ещё раз.")
