import asyncio

import pytest

from notes_bot.gemini import GeminiError
from notes_bot.notes import NoteDocument
from notes_bot.outline import blocks_of, fixed, marked, plan
from notes_bot.pipeline import NotesPipeline, Source
from notes_bot.settings import UserSettings


def _long_text(paragraphs=400):
    return "\n".join(f"Абзац номер {n} о теме {n // 50}, с тремя словами и числом {n * 7}." for n in range(paragraphs))


def test_blocks_keep_paragraphs_whole_and_are_about_the_size():
    text = _long_text(300)
    blocks = blocks_of(text, size=1000)
    assert len(blocks) > 5
    assert "\n".join(blocks).split("\n") == text.split("\n")  # nothing lost, nothing retyped
    assert all(len(block) < 1100 for block in blocks)


def test_marked_text_numbers_the_blocks():
    assert marked(["первый", "второй"]).startswith("[блок 0]\nпервый\n\n[блок 1]")


def test_the_model_starts_become_parts_and_the_text_is_cut_not_rewritten():
    blocks = [f"блок {n}" for n in range(6)]
    parts = plan([{"title": "Введение", "from": 0}, {"title": "Механика", "from": 2}, {"title": "Оптика", "from": 4}],
                 blocks, limit=10_000)
    assert [part.title for part in parts] == ["Введение", "Механика", "Оптика"]
    assert parts[0].text == "блок 0\n\nблок 1" and parts[2].text == "блок 4\n\nблок 5"


def test_unusable_answers_fall_back_to_equal_parts():
    blocks = [f"блок {n} " + "x" * 90 for n in range(40)]
    for raw in ([], "не список", [{"title": "", "from": 3}], [{"title": "Тема", "from": 999}]):
        parts = plan(raw, blocks, limit=500)
        assert len(parts) > 1 and parts[0].title == "Часть 1"
        assert sum(len(part.text) for part in parts) >= sum(len(block) for block in blocks) - 40 * 2


def test_a_topic_longer_than_the_limit_is_cut_into_parts():
    blocks = [f"блок {n} " + "x" * 90 for n in range(30)]
    parts = plan([{"title": "Огромная тема", "from": 0}], blocks, limit=500)
    assert len(parts) > 1 and parts[0].title == "Огромная тема, часть 1"


class SplittingModel:
    def __init__(self, outline=None, fail=False):
        self.outline_answer, self.fail = outline, fail
        self.written: list[str] = []

    async def transcribe(self, audio, filename):
        return ""

    async def outline(self, marked_text):
        if self.fail:
            raise GeminiError("Не получилось разбить материал на темы.")
        return self.outline_answer

    async def write(self, transcript, length_id, language="auto", extras=()):
        self.written.append(transcript)
        return NoteDocument(title="Тема", main_idea="Идея.", sections=[{"heading": "Р", "elements": [
            {"type": "bullets", "items": ["пункт"]}]}])


async def _progress(stage, detail=""):
    return None


def _run_parts(model, settings, **kwargs):
    return asyncio.run(NotesPipeline(model).run_parts(settings, progress=_progress, **kwargs))


def test_a_long_semester_text_gets_one_note_per_topic(monkeypatch):
    import notes_bot.pipeline as pipeline

    text = _long_text(3000)  # well over SPLIT_CHARS
    blocks = blocks_of(text)
    model = SplittingModel(outline=[{"title": "Первая", "from": 0}, {"title": "Вторая", "from": len(blocks) // 2}])
    results = _run_parts(model, UserSettings(output="pdf"), text=text)
    assert len(results) == 2 and len(model.written) == 2
    assert model.written[0] + "\n\n" + model.written[1] == "\n\n".join(blocks).strip()


def test_split_on_request_makes_topics_even_for_a_short_text():
    model = SplittingModel(outline=[{"title": "Одна", "from": 0}, {"title": "Другая", "from": 1}])
    short = "\n".join(f"Короткая строка номер {n} о {n % 3} разных вещах." for n in range(120))
    assert len(_run_parts(model, UserSettings(output="pdf"), text=short)) == 1  # no request: one note
    model = SplittingModel(outline=[{"title": "Одна", "from": 0}, {"title": "Другая", "from": 1}])
    results = _run_parts(model, UserSettings(output="pdf"), sources=[Source(text=short, split=True)])
    assert len(results) == 2


def test_if_the_outline_fails_the_text_is_still_split_into_notes():
    text = _long_text(3000)
    model = SplittingModel(fail=True)
    results = _run_parts(model, UserSettings(output="pdf"), text=text)
    assert len(results) > 1 and len(model.written) == len(results)


def test_fixed_parts_cover_the_whole_text():
    blocks = blocks_of(_long_text(400), size=1000)
    parts = fixed(blocks, limit=5000)
    assert "".join(part.text.replace("\n", "") for part in parts) == "".join(block.replace("\n", "") for block in blocks)
