import asyncio
from types import SimpleNamespace

import httpx
import pytest

from notes_bot.llm import NotesError, NotesWriter
from notes_bot.menus import apply_choice, build_view
from notes_bot.notes import NoteDocument
from notes_bot.pipeline import NotesPipeline
from notes_bot.settings import UserSettings
from notes_bot.transcribe import Transcriber, TranscriptionError


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _writer_with(response) -> tuple[NotesWriter, FakeMessages]:
    writer = NotesWriter("key", "claude-test")
    messages = FakeMessages(response)
    writer._client = SimpleNamespace(messages=messages)
    return writer, messages


TOOL_INPUT = {
    "title": "Тема",
    "icon": "🧪",
    "concepts": [{"term": "Термин", "definition": "Определение.", "why": "Зачем"}],
    "takeaways": ["Вывод"],
}


def test_writer_forces_the_tool_and_validates_output():
    response = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="save_notes", input=TOOL_INPUT)],
    )
    writer, messages = _writer_with(response)
    doc = asyncio.run(writer.write("материал " * 50, ["concepts", "takeaways"], "short"))

    call = messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "save_notes"}
    assert set(call["tools"][0]["input_schema"]["properties"]) == {"title", "icon", "concepts", "takeaways"}
    assert doc.title == "Тема"
    assert doc.concepts[0].term == "Термин"


def test_writer_reports_truncated_answers():
    writer, _ = _writer_with(SimpleNamespace(stop_reason="max_tokens", content=[]))
    with pytest.raises(NotesError, match="не уместился"):
        asyncio.run(writer.write("материал " * 50, ["takeaways"], "long"))


def test_writer_reports_missing_tool_call():
    writer, _ = _writer_with(SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text")]))
    with pytest.raises(NotesError):
        asyncio.run(writer.write("материал " * 50, ["takeaways"], "medium"))


def test_transcriber_posts_multipart_and_returns_text():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = request.read()
        return httpx.Response(200, text="  Расшифровка лекции.  ")

    text = asyncio.run(Transcriber("sk-test", "whisper-1", transport=httpx.MockTransport(handler)).transcribe(b"ogg", "voice.ogg"))
    assert text == "Расшифровка лекции."
    assert seen["auth"] == "Bearer sk-test"
    assert b'name="model"' in seen["body"] and b"whisper-1" in seen["body"]
    assert b'filename="voice.ogg"' in seen["body"]


def test_transcriber_turns_http_errors_into_user_errors():
    transport = httpx.MockTransport(lambda request: httpx.Response(413, text="too big"))
    with pytest.raises(TranscriptionError, match="413"):
        asyncio.run(Transcriber("sk", transport=transport).transcribe(b"x", "a.mp3"))


class FakeTranscriber:
    async def transcribe(self, audio, filename):
        return "Распознанный текст лекции. " * 10


class FakeWriter:
    def __init__(self):
        self.enabled = None

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
    pipeline = NotesPipeline(FakeTranscriber(), FakeWriter())
    settings = UserSettings(output="tg")
    result, stages = _run(pipeline, settings, audio=b"ogg", audio_name="voice.ogg")
    assert stages == ["transcribing", "writing", "rendering"]
    assert result.pdf is None and result.messages
    assert "Тема" in result.messages[0]


def test_pipeline_text_to_pdf():
    pipeline = NotesPipeline(None, FakeWriter())
    result, stages = _run(pipeline, UserSettings(output="pdf"), text="Текст лекции. " * 20)
    assert stages == ["writing", "rendering"]
    assert result.pdf.startswith(b"%PDF")


def test_pipeline_refuses_audio_without_transcriber():
    pipeline = NotesPipeline(None, FakeWriter())
    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY"):
        _run(pipeline, UserSettings(), audio=b"x", audio_name="a.ogg")


def test_pipeline_refuses_tiny_text():
    pipeline = NotesPipeline(None, FakeWriter())
    with pytest.raises(NotesError, match="слишком мало"):
        _run(pipeline, UserSettings(), text="привет")


def test_pipeline_only_sends_enabled_blocks_to_the_writer():
    writer = FakeWriter()
    _run(NotesPipeline(None, writer), UserSettings(blocks=("takeaways",)), text="Текст лекции. " * 20)
    assert writer.enabled == ["takeaways"]


def _button_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_every_menu_button_fits_telegram_callback_limit():
    settings = UserSettings(theme="custom", custom_color="#0f766e")
    for view in ["main", "output", "length", "theme", "blocks"]:
        _, markup = build_view(view, settings)
        for data in _button_data(markup):
            assert data is None or len(data.encode()) <= 64, data


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


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
