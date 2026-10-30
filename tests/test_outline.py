import asyncio

import pytest

from notes_bot.gemini import GeminiError
from notes_bot.notes import NoteDocument
from notes_bot.outline import MIN_PART_CHARS, fixed, marked, paragraphs_of, plan
from notes_bot.pipeline import NotesPipeline, Source
from notes_bot.settings import UserSettings


def _long_text(paragraphs=400):
    return "\n".join(f"Абзац номер {n} о теме {n // 50}, с тремя словами и числом {n * 7}." for n in range(paragraphs))


def test_every_non_empty_line_is_a_paragraph_the_model_can_point_at():
    text = "Первый абзац.\n\n  \nВторой абзац.\n"
    assert paragraphs_of(text) == ["Первый абзац.", "Второй абзац."]


def test_marked_text_numbers_the_paragraphs():
    assert marked(["первый", "второй"]) == "[0] первый\n[1] второй"


def test_the_model_starts_become_parts_and_the_text_is_cut_not_rewritten():
    big = "x" * MIN_PART_CHARS
    blocks = [f"блок {n} {big}" for n in range(6)]
    parts = plan([{"title": "Введение", "from": 0}, {"title": "Механика", "from": 2}, {"title": "Оптика", "from": 4}],
                 blocks, limit=10_000)
    assert [part.title for part in parts] == ["Введение", "Механика", "Оптика"]
    assert parts[0].text.startswith("блок 0") and parts[0].text.endswith("блок 1 " + big)
    assert parts[2].text.startswith("блок 4") and parts[2].text.endswith("блок 5 " + big)


def test_a_tiny_topic_joins_the_one_before_it():
    big = "x" * (MIN_PART_CHARS + 10)
    blocks = [f"абзац {n} {big}" for n in range(3)]
    parts = plan([{"title": "Большая", "from": 0}, {"title": "Короткая", "from": 2}], blocks[:2] + ["кусок"],
                 limit=10_000)
    assert [part.title for part in parts] == ["Большая"]
    assert parts[0].text.endswith("кусок")


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

    async def write(self, transcript, length_id, language="auto", extras=(), part=""):
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
    blocks = paragraphs_of(text)
    model = SplittingModel(outline=[{"title": "Первая", "from": 0}, {"title": "Вторая", "from": len(blocks) // 2}])
    results = _run_parts(model, UserSettings(output="pdf", length="medium"), text=text)
    assert len(results) == 2 and len(model.written) == 2
    assert model.written[0] + "\n\n" + model.written[1] == "\n\n".join(blocks).strip()


def test_split_on_request_makes_topics_even_for_a_short_text():
    short = "\n".join(f"Строка номер {n} о {n % 3} разных вещах и примере, которым заканчивается абзац." for n in range(120))
    outline = [{"title": "Одна", "from": 0}, {"title": "Другая", "from": 60}]
    model = SplittingModel(outline=outline)
    assert len(_run_parts(model, UserSettings(output="pdf"), text=short)) == 1  # no request: one note
    model = SplittingModel(outline=outline)
    results = _run_parts(model, UserSettings(output="pdf"), sources=[Source(text=short, split=True)])
    assert len(results) == 2


def test_if_the_outline_fails_the_text_is_still_split_into_notes():
    text = _long_text(3000)
    model = SplittingModel(fail=True)
    results = _run_parts(model, UserSettings(output="pdf", length="medium"), text=text)
    assert len(results) > 1 and len(model.written) == len(results)


def test_fixed_parts_cover_the_whole_text():
    blocks = paragraphs_of(_long_text(400))
    parts = fixed(blocks, limit=5000)
    assert "".join(part.text.replace("\n", "") for part in parts) == "".join(block.replace("\n", "") for block in blocks)


def test_without_merging_every_topic_the_model_found_keeps_its_own_note():
    big = "x" * 300
    blocks = [f"абзац {n} {big}" for n in range(3)]
    raw = [{"title": "Первая", "from": 0}, {"title": "Вторая", "from": 1}, {"title": "Третья", "from": 2}]
    assert [part.title for part in plan(raw, blocks, merge=False)] == ["Первая", "Вторая", "Третья"]
    assert [part.title for part in plan(raw, blocks)] == ["Первая"]


def test_pieces_cut_at_paragraphs_and_inside_a_huge_paragraph_at_sentences():
    from notes_bot.outline import pieces

    huge = " ".join(f"Предложение номер {number} о репутации." for number in range(400))
    text = "Короткий абзац.\n" + huge + "\nПоследний абзац."
    chunks = pieces(text, 3000)
    assert len(chunks) > 3
    assert all(len(chunk) <= 3100 for chunk in chunks)
    assert "Предложение номер 399 о репутации." in chunks[-1] or "Предложение номер 399 о репутации." in chunks[-2]
    assert "".join(chunks).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


class PiecewiseModel(SplittingModel):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str, tuple]] = []

    async def write(self, transcript, length_id, language="auto", extras=(), part=""):
        self.calls.append((length_id, part, tuple(extras)))
        if length_id == "closing":
            return NoteDocument(title="Вся лекция", main_idea="Общая идея.", sections=[{"heading": "Что запомнить", "elements": [
                {"type": "checklist", "items": ["главное"]}]}] if extras else [])
        return NoteDocument(title=f"Часть {part}", main_idea="Идея части.", sections=[{"heading": f"Раздел {part}", "elements": [
            {"type": "paragraph", "text": transcript[:50]}]}])


def test_a_long_lecture_in_maximum_detail_is_written_in_pieces_and_joined():
    text = "\n".join(f"Абзац {number}: репутация складывается из мнений разных аудиторий и их опыта." for number in range(700))
    model = PiecewiseModel()
    result = _run_parts(model, UserSettings(output="pdf", length="max", extras=("remember",)), text=text)[0]
    pieces_written = [call for call in model.calls if call[0] == "max"]
    assert len(pieces_written) > 1 and all(call[2] == () for call in pieces_written)  # extras only once, at the end
    assert model.calls[-1] == ("closing", "", ("remember",))
    assert result.document.title == "Вся лекция" and result.document.main_idea == "Общая идея."
    assert [s.heading for s in result.document.sections] == [
        *(f"Раздел {n} из {len(pieces_written)}" for n in range(1, len(pieces_written) + 1)), "Что запомнить"]


def test_a_short_lecture_or_a_shorter_length_is_written_in_one_go():
    text = "\n".join(f"Абзац {number}: репутация складывается из мнений разных аудиторий и их опыта." for number in range(700))
    model = PiecewiseModel()
    _run_parts(model, UserSettings(output="pdf", length="medium"), text=text)
    assert [call[0] for call in model.calls] == ["medium"]
    model = PiecewiseModel()
    _run_parts(model, UserSettings(output="pdf", length="max"), text=text[:10_000])
    assert [call[0] for call in model.calls] == ["max"]


def test_the_note_is_asked_for_a_size_range_and_longer_lengths_never_overlap_shorter_ones():
    from notes_bot.blocks import LENGTHS
    from notes_bot.notes import build_user_message

    message = build_user_message("слово " * 10_000, "max")
    assert "от 14400 до 20400 знаков" in message and "воду" in message
    assert all(shorter.high <= longer.low for shorter, longer in zip(LENGTHS, LENGTHS[1:]))
    assert "часть 2 из 5" in build_user_message("текст", "full", part="2 из 5")
