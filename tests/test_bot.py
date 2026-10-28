import asyncio
from pathlib import Path
from types import SimpleNamespace

from notes_bot.bot import KEY_SAVED, NO_KEY, Services, on_forgetkey, on_media, on_setkey, on_settings_button, on_text
from notes_bot.config import Config
from notes_bot.gemini import GeminiError
from notes_bot.keys import KeyVault
from notes_bot.notes import NoteDocument
from notes_bot.settings import SettingsStore, UserSettings

SAVED_KEY = "AIzaSyTestKeyValue1234567890abcdefghij"
REJECTED_KEY = "AIzaSyTestKeyValue1234567890abcdefBAD"
LONG_TEXT = "Лекция о рекламе и её признаках. " * 10


class FakeModel:
    def __init__(self, api_key: str, fail_write: Exception | None = None) -> None:
        self.api_key = api_key
        self.fail_write = fail_write

    async def check_key(self) -> None:
        if self.api_key.endswith("BAD"):
            raise GeminiError("Gemini не принял ключ или у него нет доступа к модели. Проверьте ключ в Google AI Studio.")

    async def transcribe(self, audio, filename):
        return LONG_TEXT

    async def write(self, transcript, enabled, length):
        if self.fail_write is not None:
            raise self.fail_write
        return NoteDocument(title="Лекция", takeaways=["Главный вывод " * 4], questions=["Вопрос?"])


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


def _async(value):
    async def call():
        return value

    return call


def _context(tmp_path, *, fail_write: Exception | None = None, allowed=frozenset()):
    config = Config(
        telegram_token="t",
        gemini_model="m",
        data_dir=tmp_path,
        allowed_user_ids=allowed,
    )
    services = Services(
        config=config,
        store=SettingsStore(tmp_path / "s.sqlite3"),
        keys=KeyVault(tmp_path),
        make_model=lambda api_key: FakeModel(api_key, fail_write),
        locks={},
    )
    return SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}), user_data={}), services


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


def test_setkey_then_key_message_is_checked_and_saved(tmp_path):
    context, services = _context(tmp_path)
    asyncio.run(on_setkey(_update(4, message=FakeMessage(text="/setkey")), context))
    assert context.user_data["awaiting"] == "key"

    message = FakeMessage(text=SAVED_KEY)
    asyncio.run(on_text(_update(4, message=message), context))

    assert services.keys.load(4) == SAVED_KEY
    assert "awaiting" not in context.user_data
    assert message.replies[-1]["text"] == KEY_SAVED


def test_key_pasted_without_the_command_is_detected(tmp_path):
    context, services = _context(tmp_path)
    asyncio.run(on_text(_update(4, message=FakeMessage(text=SAVED_KEY)), context))
    assert services.keys.load(4) == SAVED_KEY


def test_key_in_the_newer_format_is_detected_too(tmp_path):
    context, services = _context(tmp_path)
    key = "AQ.TestKeyValue1234567890abcdefghijklmnop"
    asyncio.run(on_text(_update(4, message=FakeMessage(text=key)), context))
    assert services.keys.load(4) == key


def test_rejected_key_is_not_saved_and_the_user_can_retry(tmp_path):
    context, services = _context(tmp_path)
    asyncio.run(on_setkey(_update(4, message=FakeMessage(text="/setkey")), context))

    message = FakeMessage(text=REJECTED_KEY)
    asyncio.run(on_text(_update(4, message=message), context))

    assert services.keys.load(4) is None
    assert "не принял ключ" in message.replies[-1]["text"]
    assert context.user_data["awaiting"] == "key"


def test_forgetkey_removes_the_saved_key(tmp_path):
    context, services = _context(tmp_path)
    services.keys.save(4, SAVED_KEY)
    asyncio.run(on_forgetkey(_update(4, message=FakeMessage(text="/forgetkey")), context))
    assert services.keys.load(4) is None


def test_transcript_without_a_key_asks_for_one(tmp_path):
    context, _ = _context(tmp_path)
    message = FakeMessage(text=LONG_TEXT)
    asyncio.run(on_text(_update(2, message=message), context))
    assert message.replies == [{"text": NO_KEY}]
    assert not message.edits


def test_text_document_becomes_pdf_with_the_users_key(tmp_path):
    context, services = _context(tmp_path)
    services.keys.save(5, SAVED_KEY)
    body = LONG_TEXT.encode("utf-8")

    async def get_file():
        return SimpleNamespace(download_as_bytearray=_async(bytearray(body)))

    document = SimpleNamespace(file_name="lecture.txt", file_size=len(body), get_file=get_file)
    message = FakeMessage(document=document)
    asyncio.run(on_media(_update(5, message=message), context))

    assert any(reply.get("filename") == "Лекция.pdf" for reply in message.replies)
    pdf = next(reply["document"] for reply in message.replies if "document" in reply)
    assert pdf.startswith(b"%PDF")
    assert message.edits[-1] == "✅ Конспект готов"


def test_failures_are_reported_to_the_user(tmp_path):
    context, services = _context(tmp_path, fail_write=GeminiError("Модель не вернула конспект. Попробуйте ещё раз."))
    services.keys.save(2, SAVED_KEY)
    message = FakeMessage(text=LONG_TEXT)
    asyncio.run(on_text(_update(2, message=message), context))
    assert message.edits[-1].startswith("😕 Модель не вернула конспект")


def test_access_list_blocks_strangers(tmp_path):
    context, _ = _context(tmp_path, allowed=frozenset({100}))
    message = FakeMessage(text=LONG_TEXT)
    asyncio.run(on_text(_update(999, message=message), context))
    assert "Доступ закрыт" in message.replies[-1]["text"]
    assert "999" in message.replies[-1]["text"]
    assert not message.edits


def test_telegram_messages_go_out_as_markdown_v2(tmp_path):
    context, services = _context(tmp_path)
    services.keys.save(3, SAVED_KEY)
    services.store.save(3, UserSettings(output="tg"))
    message = FakeMessage(text=LONG_TEXT)
    asyncio.run(on_text(_update(3, message=message), context))
    sent = [reply for reply in message.replies if "text" in reply and reply.get("parse_mode")]
    assert sent, message.replies
    assert all(reply["parse_mode"] == "MarkdownV2" for reply in sent)
    assert Path(tmp_path / "s.sqlite3").exists()
