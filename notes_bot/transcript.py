"""Spot a "transcript" that is not one: a model that never received the audio answers with requests
for a file or with filler, and a note built from that would be nonsense."""
from __future__ import annotations

import re
from collections import Counter

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
# "Please provide / attach the audio, video, recording, file or text", in any of the usual wordings.
_ASK = re.compile(
    r"(предоставьте|прикрепите|пришлите|загрузите|отправьте)[^.\n]{0,60}(аудио|видео|запис|файл|текст)"
    r"|без исходного материала|не могу выполнить вашу просьб",
    re.IGNORECASE,
)
MIN_LINES_FOR_REPETITION = 6
MAX_REPEATED_SHARE = 0.5
# Words and word pairs ("за- не") looping on one line, as a model does when it degenerates.
MIN_WORDS_FOR_LOOP = 40
MAX_UNIQUE_WORD_SHARE = 0.2
MAX_TOP_BIGRAM_SHARE = 0.3


def junk_reason(text: str) -> str | None:
    """Why this text cannot be a transcript, or None if it looks like one."""
    lowered = text.lower()
    asks = sum(lowered.count(phrase) for phrase in _NO_AUDIO) + len(_ASK.findall(text))
    if asks >= 2 or (asks == 1 and len(text) < 400):
        return "вместо расшифровки модель просит прислать запись"
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    if len(lines) >= MIN_LINES_FOR_REPETITION and len(set(lines)) / len(lines) < MAX_REPEATED_SHARE:
        return "расшифровка состоит из повторяющихся строк"
    words = re.findall(r"\w+", lowered)
    if len(words) >= MIN_WORDS_FOR_LOOP:
        if len(set(words)) / len(words) < MAX_UNIQUE_WORD_SHARE:
            return "расшифровка состоит из повторяющихся слов"
        pairs = Counter(zip(words, words[1:]))
        if pairs.most_common(1)[0][1] / (len(words) - 1) > MAX_TOP_BIGRAM_SHARE:
            return "расшифровка состоит из повторяющихся слов"
    return None


def strip_requests(text: str) -> str:
    """Drop the paragraphs where the model asks for a file, keeping what it did hear."""
    kept = [part for part in re.split(r"\n\s*\n", text) if not _ASK.search(part) or len(part) > 400]
    return "\n\n".join(part.strip() for part in kept if part.strip())
