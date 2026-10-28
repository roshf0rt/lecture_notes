import asyncio

import pytest

from notes_bot.gemini import GeminiError
from notes_bot.menus import apply_choice, menu_label, settings_view
from notes_bot.notes import NoteDocument
from notes_bot.pipeline import NotesPipeline
from notes_bot.settings import UserSettings
from notes_bot.ui import Ui

PLAIN = Ui(premium=False)
PREMIUM = Ui(premium=True)

LONG_TRANSCRIPT = "Распознанный текст лекции о рекламе. " * 10


class FakeModel:
    def __init__(self) -> None:
        self.enabled: list[str] | None = None

    async def transcribe(self, audio, filename):
        return LONG_TRANSCRIPT

    async def write(self, transcript, enabled, length, language="auto"):
        self.language = language
        self.enabled = list(enabled)
        return NoteDocument(title="Тема", concepts=[], takeaways=["Вывод " * 5], questions=["Вопрос?"])


def _run(pipeline, settings, **kwargs):
    stages = []

    async def progress(stage, detail=""):
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
    for view in ["main", "output", "length", "language", "theme", "blocks"]:
        _, markup = settings_view(PREMIUM, view, settings)
        for data in _button_data(markup):
            assert data is None or len(data.encode()) <= 64, data


def test_menu_marks_current_choices():
    settings = UserSettings(output="tg")
    _, markup = settings_view(PLAIN, "output", settings)
    assert any(label.startswith("✅") and "Telegram" in label for label in _labels(markup))

    assert apply_choice(settings, "length", "long").length == "long"
    assert apply_choice(settings, "theme", "nope") == settings


def test_preset_is_marked_even_after_a_custom_colour_was_set():
    settings = UserSettings(theme="ocean", custom_color="#0f766e")
    _, markup = settings_view(PLAIN, "theme", settings)
    assert "✅ Океан" in _labels(markup)
    assert not any(label.startswith("✅ Свой цвет") for label in _labels(markup))


def test_premium_buttons_carry_the_icon_outside_the_text():
    _, markup = settings_view(PREMIUM, "output", UserSettings(output="pdf"))
    button = markup.inline_keyboard[0][0]
    assert button.text == "PDF-файл"
    assert button.icon_custom_emoji_id.isdigit()
    assert button.style == "success"


def test_plain_buttons_keep_the_emoji_in_the_text():
    _, markup = settings_view(PLAIN, "output", UserSettings(output="pdf"))
    button = markup.inline_keyboard[0][0]
    assert button.text == "✅ PDF-файл"
    assert button.icon_custom_emoji_id is None


def test_premium_message_icons_are_tg_emoji_tags():
    text, _ = settings_view(PREMIUM, "main", UserSettings())
    assert '<tg-emoji emoji-id="6032742198179532882">⚙</tg-emoji>' in text


@pytest.mark.parametrize(("text", "expected"), [("Настройки", "Настройки"), ("⚙ Настройки", "Настройки"),
                                                ("👑 Админка", "Админка"), ("Настройки лекции", None),
                                                ("Мне Помощь", None), ("⚙️ Настройки", "Настройки")])
def test_reply_keyboard_presses_are_recognised(text, expected):
    assert menu_label(text) == expected


def test_pipeline_passes_language_and_returns_the_transcript():
    model = FakeModel()
    result, _ = _run(NotesPipeline(model), UserSettings(output="tg", language="ru"), audio=b"ogg", audio_name="voice.ogg")
    assert model.language == "ru"
    assert result.transcript == LONG_TRANSCRIPT


def test_settings_round_trip_keeps_new_fields():
    settings = UserSettings(language="ru", send_transcript=True)
    assert UserSettings.from_dict(settings.to_dict()) == settings
    assert UserSettings.from_dict({"language": "xx"}).language == "auto"


def test_premium_can_be_stripped_from_a_message_and_keyboard():
    from notes_bot.menus import main_keyboard
    from notes_bot.ui import without_premium

    text, markup = settings_view(PREMIUM, "output", UserSettings(output="pdf"))
    plain_text, plain_markup = without_premium(text, markup)
    assert "<tg-emoji" not in plain_text and "📁" in plain_text
    first = plain_markup.inline_keyboard[0][0]
    assert first.text == "✅ PDF-файл" and first.icon_custom_emoji_id is None

    _, reply_markup = without_premium("", main_keyboard(PREMIUM, True))
    assert reply_markup.keyboard[0][0].text == "⚙ Настройки"


def test_a_failing_part_is_named_in_the_error():
    class BrokenSecond(FakeModel):
        async def transcribe(self, audio, filename):
            if audio == b"bad":
                raise GeminiError("запись повреждена")
            return LONG_TRANSCRIPT

    from notes_bot.pipeline import Source

    sources = [Source(audio=b"ogg", name="a.ogg"), Source(audio=b"bad", name="b.ogg")]
    with pytest.raises(GeminiError, match=r"Файл 2 \(b\.ogg\): запись повреждена"):
        _run(NotesPipeline(BrokenSecond()), UserSettings(), sources=sources)
