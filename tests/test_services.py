import asyncio

import pytest

from notes_bot.gemini import GeminiError
from notes_bot.menus import apply_choice, build_view
from notes_bot.notes import NoteDocument
from notes_bot.pipeline import NotesPipeline
from notes_bot.settings import UserSettings

LONG_TRANSCRIPT = "Распознанный текст лекции о рекламе. " * 10


class FakeModel:
    def __init__(self) -> None:
        self.enabled: list[str] | None = None

    async def transcribe(self, audio, filename):
        return LONG_TRANSCRIPT

    async def write(self, transcript, enabled, length):
        self.enabled = list(enabled)
        return NoteDocument(title="Тема", concepts=[], takeaways=["Вывод " * 5], questions=["Вопрос?"])


def _run(pipeline, settings, **kwargs):
    stages = []

    async def progress(stage):
        stages.append(stage)

    result = asyncio.run(pipeline.run(settings, progress=progress, **kwargs))
    return result, stages


def test_pipeline_audio_to_telegram_messages():
    result, stages = _run(NotesPipeline(FakeModel()), UserSettings(output="tg"), audio=b"ogg", audio_name="voice.ogg")
    assert stages == ["transcribing", "writing", "rendering"]
    assert result.pdf is None and result.messages
    assert "Тема" in result.messages[0]


def test_pipeline_text_to_pdf():
    result, stages = _run(NotesPipeline(FakeModel()), UserSettings(output="pdf"), text=LONG_TRANSCRIPT)
    assert stages == ["writing", "rendering"]
    assert result.pdf.startswith(b"%PDF")


def test_pipeline_refuses_tiny_text():
    with pytest.raises(GeminiError, match="слишком мало"):
        _run(NotesPipeline(FakeModel()), UserSettings(), text="привет")


def test_pipeline_only_sends_enabled_blocks_to_the_model():
    model = FakeModel()
    _run(NotesPipeline(model), UserSettings(blocks=("takeaways",)), text=LONG_TRANSCRIPT)
    assert model.enabled == ["takeaways"]


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _button_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_every_menu_button_fits_telegram_callback_limit():
    settings = UserSettings(theme="custom", custom_color="#0f766e")
    for view in ["main", "output", "length", "theme", "blocks"]:
        _, markup = build_view(view, settings)
        for data in _button_data(markup):
            assert data is None or len(data.encode()) <= 64, data


def test_menu_marks_current_choices():
    settings = UserSettings(output="tg")
    _, markup = build_view("output", settings)
    assert any(label.startswith("✅") and "Telegram" in label for label in _labels(markup))

    assert apply_choice(settings, "length", "long").length == "long"
    assert apply_choice(settings, "theme", "nope") == settings


def test_preset_is_marked_even_after_a_custom_colour_was_set():
    settings = UserSettings(theme="ocean", custom_color="#0f766e")
    _, markup = build_view("theme", settings)
    assert "✅ Океан" in _labels(markup)
    assert not any(label.startswith("✅ Свой цвет") for label in _labels(markup))
