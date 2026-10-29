"""Which group topic a note most likely belongs to. The user has the last word: this only orders the buttons."""
from __future__ import annotations

# Subjects are matched by their first letters, so "Физика" also catches "физике" and "физику".
STEM = 5
MIN_STEM = 4


def suggest(subjects: list[str], note_text: str) -> tuple[list[str], int]:
    """Subjects with the ones whose words occur in the note first. Also returns how many matched."""
    lowered = note_text.lower()

    def matches(subject: str) -> bool:
        stem = subject.strip().lower()[:STEM]
        return len(stem) >= MIN_STEM and stem in lowered

    matched = [subject for subject in subjects if matches(subject)]
    return matched + [subject for subject in subjects if subject not in matched], len(matched)
