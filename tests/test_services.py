import asyncio

import pytest

from notes_bot.blocks import DEFAULT_LENGTH
from notes_bot.gemini import GeminiError
from notes_bot.menus import apply_choice, menu_label, settings_view
from notes_bot.notes import NoteDocument
from notes_bot.pipeline import NotesPipeline
from notes_bot.settings import UserSettings
from notes_bot.ui import Ui

PLAIN = Ui(premium=False)
PREMIUM = Ui(premium=True)

LONG_TRANSCRIPT = (
    "Распознанный текст лекции о рекламе: как она устроена и чем отличается от информации. "
    "Реклама пытается изменить решение человека, а информация сообщает факт. "
    "Событийный маркетинг использует живой опыт, мероприятие и встречу с брендом. "
    "Участник запоминает не баннер, а собственное впечатление от происходящего. "
)


class FakeModel:
    async def transcribe(self, audio, filename):
        return LONG_TRANSCRIPT

    async def write(self, transcript, length, language="auto", extras=()):
        self.language = language
        return _note()


def _note(main_idea="", count=1) -> NoteDocument:
    return NoteDocument(title="Тема", main_idea=main_idea, sections=[
        {"heading": f"Раздел {n}", "elements": [{"type": "bullets", "items": ["Вывод " * 5]}]} for n in range(count)
    ])


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


def test_an_empty_note_is_an_error_not_a_blank_document():
    class Empty(FakeModel):
        async def write(self, transcript, length, language="auto", extras=()):
            return NoteDocument(title="Пусто")

    with pytest.raises(GeminiError, match="пустой конспект"):
        _run(NotesPipeline(Empty()), UserSettings(), text=LONG_TRANSCRIPT)


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _button_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_every_menu_button_fits_telegram_callback_limit():
    settings = UserSettings(theme="custom", custom_color="#0f766e")
    for view in ["main", "output", "length", "language", "theme", "extras"]:
        _, markup = settings_view(PREMIUM, view, settings)
        for data in _button_data(markup):
            assert data is None or len(data.encode()) <= 64, data


def test_menu_marks_current_choices():
    settings = UserSettings(output="tg")
    _, markup = settings_view(PLAIN, "output", settings)
    assert any(label.startswith("✅") and "Только полный текст в чате" in label for label in _labels(markup))

    assert apply_choice(settings, "length", "long").length == "long"
    assert apply_choice(settings, "theme", "nope") == settings


def test_preset_is_marked_even_after_a_custom_colour_was_set():
    settings = UserSettings(theme="ocean", custom_color="#0f766e")
    _, markup = settings_view(PLAIN, "theme", settings)
    assert "✅ Океан" in _labels(markup)
    assert not any(label.startswith("✅ Свой цвет") for label in _labels(markup))


def test_premium_buttons_carry_the_icon_outside_the_text():
    _, markup = settings_view(PREMIUM, "output", UserSettings(output="pdf"))
    button = markup.inline_keyboard[2][0]
    assert button.text == "Только PDF"
    assert button.icon_custom_emoji_id.isdigit()
    assert button.style == "success"


def test_plain_buttons_keep_the_emoji_in_the_text():
    _, markup = settings_view(PLAIN, "output", UserSettings(output="pdf"))
    button = markup.inline_keyboard[2][0]
    assert button.text == "✅ Только PDF"
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
    first = plain_markup.inline_keyboard[2][0]
    assert first.text == "✅ Только PDF" and first.icon_custom_emoji_id is None

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



def test_config_reads_the_local_api_address(monkeypatch):
    from notes_bot.config import Config

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:a")
    monkeypatch.delenv("TELEGRAM_API_URL", raising=False)
    assert Config.from_env().local_mode is False
    monkeypatch.setenv("TELEGRAM_API_URL", " http://127.0.0.1:8081/ ")
    config = Config.from_env()
    assert config.local_mode and config.telegram_api_url == "http://127.0.0.1:8081"


def test_application_talks_to_the_local_server_when_configured(tmp_path):
    from notes_bot.bot import build_application
    from notes_bot.config import Config

    config = Config("123:abc", "m", tmp_path, frozenset(), telegram_api_url="http://127.0.0.1:8081")
    bot = build_application(config).bot
    assert bot.base_url == "http://127.0.0.1:8081/bot123:abc"
    assert bot.base_file_url == "http://127.0.0.1:8081/file/bot123:abc"
    assert bot.local_mode is True
    cloud = build_application(Config("123:abc", "m", tmp_path / "c", frozenset())).bot
    assert cloud.base_url == "https://api.telegram.org/bot123:abc" and cloud.local_mode is False


def test_welcome_and_help_name_the_real_file_limit():
    from notes_bot.menus import help_text, welcome

    assert "до 20 МБ" in welcome(PLAIN, True, False)
    assert "до 2 ГБ" in welcome(PLAIN, True, False, local_api=True)
    assert "до 2 ГБ" in help_text(PLAIN, local_api=True)
    assert "20 МБ" not in welcome(PLAIN, True, False, local_api=True)


def test_both_outputs_are_the_default_and_produce_pdf_and_messages():
    assert UserSettings().output == "both"
    model = FakeModel()
    result, _ = _run(NotesPipeline(model), UserSettings(), text=LONG_TRANSCRIPT)
    assert result.pdf.startswith(b"%PDF") and result.messages
    tg_only, _ = _run(NotesPipeline(model), UserSettings(output="tg"), text=LONG_TRANSCRIPT)
    assert tg_only.pdf is None and tg_only.messages


def test_one_message_output_asks_for_the_short_length_and_fits_one_message():
    seen = {}

    class LengthModel(FakeModel):
        async def write(self, transcript, length, language="auto", extras=()):
            seen["length"] = length
            return _note("Главная идея. " * 10, 2)

    result, _ = _run(NotesPipeline(LengthModel()), UserSettings(output="tg_one"), text=LONG_TRANSCRIPT)
    assert seen["length"] == "onepage"
    assert len(result.messages) == 1 and result.pdf is None


def test_a_long_note_loses_its_last_sections_to_fit_one_message():
    from notes_bot.render_tg import TG_LIMIT, fit_one_message

    big = NoteDocument(
        title="Большая лекция",
        main_idea="Главная идея. " * 40,
        sections=[
            {"heading": f"Раздел {n}", "elements": [{"type": "paragraph", "text": "Объяснение механизма. " * 30}]}
            for n in range(12)
        ],
    )
    messages = fit_one_message(big)
    assert len(messages) == 1 and len(messages[0]) <= TG_LIMIT
    assert "Главная идея" in messages[0] and "Раздел 0" in messages[0] and "Раздел 11" not in messages[0]


def test_the_short_length_is_not_a_choice_the_user_can_save():
    from notes_bot.settings import UserSettings as Settings

    assert Settings.from_dict({"length": "onepage"}).length == DEFAULT_LENGTH
    assert Settings.from_dict({"output": "tg_one"}).output == "tg_one"
    _, markup = settings_view(PLAIN, "output", UserSettings(output="tg_one"))
    assert any("одно короткое сообщение" in label for label in _labels(markup))


def test_extras_reach_the_model_but_not_the_one_message_format():
    seen = []

    class Spy(FakeModel):
        async def write(self, transcript, length, language="auto", extras=()):
            seen.append(tuple(extras))
            return _note()

    settings = UserSettings(extras=("remember", "terms"))
    _run(NotesPipeline(Spy()), settings, text=LONG_TRANSCRIPT)
    _run(NotesPipeline(Spy()), UserSettings(output="tg_one", extras=("remember",)), text=LONG_TRANSCRIPT)
    assert seen == [("remember", "terms"), ()]


def test_extras_menu_marks_the_chosen_ones():
    _, markup = settings_view(PLAIN, "extras", UserSettings(extras=("terms",)))
    labels = _labels(markup)
    assert "✅ Термины и определения" in labels and "❌ Что запомнить" in labels
    assert "st:ext:questions" in _button_data(markup)


def test_pdf_with_one_short_message_writes_the_note_then_shortens_it_from_the_note():
    calls = []

    class Writer(FakeModel):
        async def write(self, transcript, length, language="auto", extras=()):
            calls.append((length, transcript, tuple(extras)))
            return _note("Главная идея. " * 3, 3)

    settings = UserSettings(output="both_one", length="long", extras=("remember",))
    result, _ = _run(NotesPipeline(Writer()), settings, text=LONG_TRANSCRIPT)
    assert result.pdf.startswith(b"%PDF") and len(result.messages) == 1
    assert [call[0] for call in calls] == ["long", "onepage"]
    assert calls[0][1] == LONG_TRANSCRIPT and calls[0][2] == ("remember",)
    assert "Раздел 0" in calls[1][1] and calls[1][2] == ()  # the second call reads the note, not the lecture


def test_if_shortening_fails_the_chat_gets_the_full_note_cut_to_one_message():
    class Flaky(FakeModel):
        async def write(self, transcript, length, language="auto", extras=()):
            if length == "onepage":
                raise GeminiError("перегрузка")
            return _note("Главная идея. " * 3, 3)

    result, _ = _run(NotesPipeline(Flaky()), UserSettings(output="both_one"), text=LONG_TRANSCRIPT)
    assert result.pdf is not None and len(result.messages) == 1 and "Раздел 0" in result.messages[0]
