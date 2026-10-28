import asyncio
from pathlib import Path
from types import SimpleNamespace

from notes_bot.bot import Services, on_media, on_settings_button, on_text
from notes_bot.config import Config
from notes_bot.gemini import GeminiError
from notes_bot.notes import NoteDocument
from notes_bot.pipeline import NotesPipeline
from notes_bot.settings import SettingsStore, UserSettings


class FakeMessage:
    def __init__(self, text=None, document=None):
        self.text = text
        self.document = document
        self.voice = None
        self.audio = None
        self.video = None
        self.video_note = None
        self.replies: list[dict] = []
        self.edits: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return FakeStatus(self)

    async def reply_document(self, document, filename=None, **kwargs):
        self.replies.append({"document": document.read(), "filename": filename})


class FakeStatus:
    def __init__(self, message):
        self._message = message

    async def edit_text(self, text, **kwargs):
        self._message.edits.append(text)


class FakeQuery:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.edited: list[tuple[str, object]] = []

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edited.append((text, reply_markup))


class FakeWriter:
    async def write(self, transcript, enabled, length):
        return NoteDocument(title="Лекция", takeaways=["Главный вывод " * 4], questions=["Вопрос?"])


def _context(tmp_path, *, writer=None, allowed=frozenset()):
    config = Config(
        telegram_token="t",
        gemini_api_key="g",
        gemini_model="m",
        data_dir=tmp_path,
        allowed_user_ids=allowed,
    )
    services = Services(
        config=config,
        store=SettingsStore(tmp_path / "s.sqlite3"),
        pipeline=NotesPipeline(writer or FakeWriter()),
        locks={},
    )
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"services": services}),
        user_data={},
    ), services


def _async(value):
    async def call():
        return value

    return call


def _update(user_id, *, query=None, message=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        callback_query=query,
    )


def test_block_toggle_is_saved_and_menu_redrawn(tmp_path):
    context, services = _context(tmp_path)
    query = FakeQuery("st:blk:examples", 1)
    asyncio.run(on_settings_button(_update(1, query=query), context))

    assert "examples" in services.store.get(1).blocks
    text, markup = query.edited[-1]
    assert "Блоки конспекта" in text
    assert any("✅" in button.text and "Примеры" in button.text for row in markup.inline_keyboard for button in row)


def test_custom_colour_flow_saves_hex_and_rejects_light_colours(tmp_path):
    context, services = _context(tmp_path)
    asyncio.run(on_settings_button(_update(1, query=FakeQuery("st:color", 1)), context))
    assert context.user_data["awaiting"] == "color"

    message = FakeMessage(text="#FFFF00")
    asyncio.run(on_text(_update(1, message=message), context))
    assert "awaiting" in context.user_data
    assert "слишком светлый" in message.replies[-1]["text"]

    message = FakeMessage(text="#0f766e")
    asyncio.run(on_text(_update(1, message=message), context))
    saved = services.store.get(1)
    assert saved.theme == "custom"
    assert saved.custom_color == "#0f766e"
    assert "awaiting" not in context.user_data


def test_text_document_becomes_pdf_with_default_settings(tmp_path):
    context, services = _context(tmp_path)
    text_body = ("Лекция о рекламе и её признаках. " * 10).encode("utf-8")

    async def get_file():
        return SimpleNamespace(download_as_bytearray=_async(bytearray(text_body)))

    document = SimpleNamespace(file_name="lecture.txt", file_size=len(text_body), get_file=get_file)
    message = FakeMessage(document=document)
    asyncio.run(on_media(_update(5, message=message), context))

    assert any(reply.get("filename") == "Лекция.pdf" for reply in message.replies)
    pdf = next(reply["document"] for reply in message.replies if "document" in reply)
    assert pdf.startswith(b"%PDF")
    assert message.edits[-1] == "✅ Конспект готов"


def test_failures_are_reported_to_the_user(tmp_path):
    class BrokenWriter:
        async def write(self, transcript, enabled, length):
            raise GeminiError("Модель не вернула конспект. Попробуйте ещё раз.")

    context, _ = _context(tmp_path, writer=BrokenWriter())
    message = FakeMessage(text="Достаточно длинный текст лекции, чтобы пройти проверку. " * 3)
    asyncio.run(on_text(_update(2, message=message), context))
    assert message.edits[-1].startswith("😕 Модель не вернула конспект")


def test_access_list_blocks_strangers(tmp_path):
    context, services = _context(tmp_path, allowed=frozenset({100}))
    message = FakeMessage(text="Текст лекции " * 20)
    asyncio.run(on_text(_update(999, message=message), context))
    assert "Доступ закрыт" in message.replies[-1]["text"]
    assert "999" in message.replies[-1]["text"]
    assert not message.edits


def test_telegram_messages_go_out_as_markdown_v2(tmp_path):
    context, _ = _context(tmp_path)
    context.application.bot_data["services"].store.save(3, UserSettings(output="tg"))
    message = FakeMessage(text="Текст лекции о рекламе и её признаках. " * 5)
    asyncio.run(on_text(_update(3, message=message), context))
    sent = [reply for reply in message.replies if "text" in reply and reply.get("parse_mode")]
    assert sent, message.replies
    assert all(reply["parse_mode"] == "MarkdownV2" for reply in sent)
    assert Path(tmp_path / "s.sqlite3").exists()
