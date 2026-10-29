"""Which group topic a note most likely belongs to. The user has the last word: this only orders the buttons."""
from __future__ import annotations

# Subjects are matched by their first letters, so "Физика" also catches "физике" and "физику".
STEM = 5
MIN_STEM = 4


def rank(subjects: list[str], note_text: str) -> tuple[list[int], int]:
    """Positions of the subjects, the ones whose words occur in the note first. Also returns how many matched.

    Positions, not names: the same subject may be bound in two groups and must keep both buttons.
    """
    lowered = note_text.lower()

    def matches(subject: str) -> bool:
        stem = subject.strip().lower()[:STEM]
        return len(stem) >= MIN_STEM and stem in lowered

    hits = [index for index, subject in enumerate(subjects) if matches(subject)]
    rest = [index for index in range(len(subjects)) if index not in hits]
    return hits + rest, len(hits)


def suggest(subjects: list[str], note_text: str) -> tuple[list[str], int]:
    order, matched = rank(subjects, note_text)
    return [subjects[index] for index in order], matched
