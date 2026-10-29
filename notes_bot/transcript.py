"""Spot a "transcript" that is not one: a model that never received the audio answers with requests
for a file or with filler, and a note built from that would be nonsense."""
from __future__ import annotations

# Phrases a model uses when it has no audio to work with.
_NO_AUDIO = (
    "прикрепите файл",
    "предоставьте аудиозапись",
    "пришлите аудиозапись",
    "прикрепите аудиозапись",
    "прикрепите файл или ссылку",
    "не вижу аудио",
    "не могу расшифровать",
    "аудиофайл не был",
    "please provide the audio",
    "please attach",
    "no audio",
    "i cannot transcribe",
)
MIN_LINES_FOR_REPETITION = 6
MAX_REPEATED_SHARE = 0.5


def junk_reason(text: str) -> str | None:
    """Why this text cannot be a transcript, or None if it looks like one."""
    lowered = text.lower()
    asks = sum(lowered.count(phrase) for phrase in _NO_AUDIO)
    if asks >= 2 or (asks == 1 and len(text) < 400):
        return "вместо расшифровки модель просит прислать запись"
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    if len(lines) >= MIN_LINES_FOR_REPETITION and len(set(lines)) / len(lines) < MAX_REPEATED_SHARE:
        return "расшифровка состоит из повторяющихся строк"
    return None
