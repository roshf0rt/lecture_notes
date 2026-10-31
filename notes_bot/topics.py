"""Which group topic a note most likely belongs to. The user has the last word: this only orders the buttons."""
from __future__ import annotations

import re

# Words of a subject are matched by their first letters, so "Физика" also catches "физике" and "физику".
STEM = 5
MIN_STEM = 4
# Words that name no subject: "Основы стилистики" must not match every note that says "основные".
GENERIC = {
    "основы", "основ", "введение", "теория", "теории", "практика", "практикум", "курс", "предмет",
    "дисциплина", "общая", "общий", "общие", "часть", "базовый", "базовые", "современные", "современный",
}
# The title and main idea say what the note is about better than a passing mention further down.
HEAD_WEIGHT = 3
# A subject is ticked only when it clearly wins: enough mentions and at least twice the next subject.
MIN_SCORE = 3
LEAD = 2.0


def _stems(subject: str) -> list[str]:
    words = re.findall(r"\w+", subject.lower())
    return [word[:STEM] for word in words if len(word) >= MIN_STEM and word not in GENERIC]


def _score(subject: str, head: str, body: str) -> int:
    score = 0
    for stem in _stems(subject):
        pattern = re.compile(rf"\b{re.escape(stem)}")
        score += HEAD_WEIGHT * len(pattern.findall(head)) + len(pattern.findall(body))
    return score


def rank(subjects: list[str], note_text: str) -> tuple[list[int], int]:
    """Positions of the subjects, the likeliest first, and how many of the first ones are a confident guess.

    Positions, not names: the same subject may be bound in two groups and must keep both buttons.
    At most one subject (by name) is a guess, and only when it clearly beats the others.
    """
    lowered = note_text.lower()
    lines = lowered.split("\n")
    head, body = "\n".join(lines[:2]), "\n".join(lines[2:])
    scores = [_score(subject, head, body) for subject in subjects]
    order = sorted(range(len(subjects)), key=lambda index: -scores[index])
    if not order or scores[order[0]] < MIN_SCORE:
        return order, 0
    leader = subjects[order[0]].strip().lower()
    runner_up = max((scores[index] for index in order if subjects[index].strip().lower() != leader), default=0)
    if scores[order[0]] < LEAD * runner_up:
        return order, 0
    return order, sum(1 for index in order if subjects[index].strip().lower() == leader)


def suggest(subjects: list[str], note_text: str) -> tuple[list[str], int]:
    order, matched = rank(subjects, note_text)
    return [subjects[index] for index in order], matched
