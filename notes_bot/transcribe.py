from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"


class TranscriptionError(Exception):
    """A user-facing error while recognising speech."""


class Transcriber:
    def __init__(
        self,
        api_key: str,
        model: str = "whisper-1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._transport = transport

    async def transcribe(self, audio: bytes, filename: str) -> str:
        # The filename's extension tells the API which audio format this is.
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0), transport=self._transport) as client:
            response = await client.post(
                OPENAI_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                data={"model": self._model, "response_format": "text"},
                files={"file": (filename, audio)},
            )
        if response.status_code != 200:
            log.warning("Transcription failed: %s %s", response.status_code, response.text[:300])
            raise TranscriptionError(f"Не удалось распознать речь (код {response.status_code}).")
        text = response.text.strip()
        if not text:
            raise TranscriptionError("В записи не нашлось речи.")
        return text
