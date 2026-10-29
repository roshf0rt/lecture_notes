import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from notes_bot import admin, bot
from notes_bot.bot import (
    on_topic_button,
    gate,
    on_batch_button,
    on_forgetkey,
    on_keys_button,
    on_media,
    on_settings_button,
    on_start,
    on_text,
)
from notes_bot.config import Config
from notes_bot.gemini import QUOTA, GeminiError
from notes_bot.keys import POOL, KeyVault
from notes_bot.notes import NoteDocument
from notes_bot.services import Services
from notes_bot.settings import SettingsStore, UserSettings
from notes_bot.storage import BotState, Options

KEY = "AIzaSyTestKeyValue1234567890abcdefghij"
KEY2 = "AQ.TestKeyValue1234567890abcdefghijklmnop"
BAD_KEY = "AIzaSyTestKeyValue1234567890abcdefBAD"
QUOTA_KEY = "AIzaSyTestKeyValue1234567890abcdeQUOTA"
LONG_TEXT = (
    "Сегодня разбираем, как устроена реклама и чем она отличается от информации. "
    "Информация сообщает факт, а реклама пытается изменить решение человека. "
    "Поэтому у рекламы есть заказчик, цель и понятный призыв к действию. "
    "Признаки рекламы: оплаченное размещение, оценочные слова, обращение к эмоциям. "
    "Событийный маркетинг использует живой опыт: мероприятие, встречу, дегустацию. "
    "Участник запоминает бренд не из баннера, а из собственного впечатления."
)
ADMIN = 900


@pytest.fixture(autouse=True)
def no_batch_wait(monkeypatch):
    monkeypatch.setattr(bot, "BATCH_WINDOW", 0)


class FakeModel:
    def __init__(self, api_key, model, log, fail_write=None):
        self.api_key = api_key
        self.model = model
        self.log = log
        self.fail_write = fail_write

    async def check_key(self):
        if self.api_key.endswith("BAD"):
            raise GeminiError("Gemini не принял ключ или у него нет доступа к модели.", "auth")
        if self.model == "broken-model":
            raise GeminiError("Выбранная модель Gemini сейчас недоступна.", "model")

    async def transcribe(self, audio, filename):
        self.log.append(("transcribe", self.api_key, filename))
        if self.api_key.endswith("QUOTA"):
            raise GeminiError("лимит", QUOTA)
        return LONG_TEXT

    async def write(self, transcript, length, language="auto", extras=()):
        self.log.append(("write", self.api_key, language))
        if self.fail_write is not None:
            raise self.fail_write
        return _note("Лекция")


def _note(title="Лекция") -> NoteDocument:
    return NoteDocument(title=title, main_idea="Главная идея лекции.", sections=[{"heading": "Тема", "elements": [{"type": "bullets", "items": ["Главный вывод " * 4]}]}])


class FakeMessage:
    def __init__(self, text=None, document=None, voice=None, audio=None, message_id=1):
        self.text = text
        self.document = document
        self.voice = voice
        self.audio = audio
        self.video = None
        self.video_note = None
        self.animation = None
        self.chat_id = 1
        self.message_id = message_id
        self.replies: list[dict] = []
        self.edits: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return FakeStatus(self)

    async def reply_document(self, document, filename=None, **kwargs):
        self.replies.append({"document": document.read(), "filename": filename})

    def texts(self):
        return [reply["text"] for reply in self.replies if "text" in reply] + self.edits


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
        self.answers: list[str] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edited.append((text, reply_markup))


class FakeBot:
    def __init__(self):
        self.copied: list[int] = []
        self.sent: list[tuple[int, str]] = []
        self.documents: list[tuple[int, int, str]] = []
        self.topic_messages: list[tuple[int, int]] = []

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copied.append(chat_id)

    async def send_message(self, chat_id, text, message_thread_id=None, **kwargs):
        self.sent.append((chat_id, text))
        self.topic_messages.append((chat_id, message_thread_id))

    async def send_document(self, chat_id, document, filename=None, message_thread_id=None, **kwargs):
        self.documents.append((chat_id, message_thread_id, filename))

    async def set_my_commands(self, *args, **kwargs):
        return True


class FakeApplication:
    def __init__(self, services):
        self.bot_data = {"services": services}
        self.tasks = []

    def create_task(self, coroutine):
        self.tasks.append(coroutine)


def _setup(tmp_path, *, fail_write=None, allowed=frozenset(), options=None):
    config = Config(telegram_token="t", gemini_model="m", data_dir=tmp_path, allowed_user_ids=allowed,
                    admin_ids=frozenset({ADMIN}))
    calls = []
    s = Services(
        config=config,
        store=SettingsStore(tmp_path / "s.sqlite3"),
        keys=KeyVault(tmp_path),
        state=BotState(tmp_path / "bot.sqlite3", options or Options(model="m"), admin_ids=config.admin_ids,
                       allowed_ids=allowed),
        make_client=lambda api_key, model: FakeModel(api_key, model, calls, fail_write),
        slots=None,
    )
    return s, calls


class Harness:
    def __init__(self, tmp_path, **kwargs):
        self.services, self.calls = _setup(tmp_path, **kwargs)
        self.application = FakeApplication(self.services)
        self.bot = FakeBot()
        self.user_data: dict[int, dict] = {}
        # Most tests are about notes, not the agreement: users arrive having accepted it.
        # Agreement tests switch this off.
        self.auto_accept = True

    def context(self, user_id):
        return SimpleNamespace(application=self.application, bot=self.bot,
                               user_data=self.user_data.setdefault(user_id, {}))

    def update(self, user_id, *, message=None, query=None):
        user = SimpleNamespace(id=user_id, username=f"user{user_id}", full_name=f"User {user_id}")
        return SimpleNamespace(effective_user=user, effective_message=message, callback_query=query,
                               effective_chat=SimpleNamespace(id=user_id))

    def text(self, user_id, text):
        message = FakeMessage(text=text)
        self.run(on_text, user_id, message=message)
        return message

    def tap(self, user_id, data, handler):
        query = FakeQuery(data, user_id)
        self.run(handler, user_id, query=query)
        return query

    def run(self, handler, user_id, **kwargs):
        asyncio.run(self._through_gate(handler, user_id, **kwargs))

    def send_together(self, user_id, messages):
        """Several files arriving at once, like an album: their handlers overlap."""
        async def all_of_them():
            await asyncio.gather(*(self._through_gate(on_media, user_id, message=message) for message in messages))

        asyncio.run(all_of_them())

    async def _through_gate(self, handler, user_id, **kwargs):
        from notes_bot.agreement import AGREEMENT_VERSION

        if self.auto_accept:
            self.services.state.accept_agreement(user_id, AGREEMENT_VERSION)
        update = self.update(user_id, **kwargs)
        context = self.context(user_id)
        try:
            await gate(update, context)
        except ApplicationHandlerStop:
            return
        await handler(update, context)
        for task in self.application.tasks:
            await task
        self.application.tasks.clear()


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# --- settings ---------------------------------------------------------------------------


def test_language_choice_and_reset(tmp_path):
    h = Harness(tmp_path)
    h.tap(1, "st:set:language:ru", on_settings_button)
    settings = h.services.store.get(1)
    assert settings.language == "ru"
    query = h.tap(1, "st:reset", on_settings_button)
    assert h.services.store.get(1) == UserSettings()
    assert "Настройки конспектов" in query.edited[-1][0]


def test_custom_colour_flow_saves_hex_and_rejects_light_colours(tmp_path):
    h = Harness(tmp_path)
    h.tap(1, "st:color", on_settings_button)
    assert h.user_data[1]["awaiting"] == "color"
    assert "слишком светлый" in h.text(1, "#FFFF00").replies[-1]["text"]
    h.text(1, "#0f766e")
    saved = h.services.store.get(1)
    assert (saved.theme, saved.custom_color) == ("custom", "#0f766e")
    assert "awaiting" not in h.user_data[1]


def test_reply_keyboard_button_opens_settings(tmp_path):
    h = Harness(tmp_path)
    message = h.text(1, "⚙ Настройки")
    assert "Настройки конспектов" in message.replies[-1]["text"]
    assert message.replies[-1]["parse_mode"] == "HTML"


def test_start_shows_the_main_keyboard_with_admin_button_only_for_admins(tmp_path):
    h = Harness(tmp_path)
    for user_id, expected in ((1, False), (ADMIN, True)):
        message = FakeMessage(text="/start")
        h.run(on_start, user_id, message=message)
        keyboard = message.replies[-1]["reply_markup"]
        labels = [button.text for row in keyboard.keyboard for button in row]
        assert ("Админка" in labels) is expected


# --- keys -------------------------------------------------------------------------------


def test_several_keys_in_one_message_are_checked_and_saved(tmp_path):
    h = Harness(tmp_path)
    message = h.text(4, f"{KEY}\n{KEY2}\n{BAD_KEY}")
    assert h.services.keys.values(4) == [KEY, KEY2]
    report = message.edits[-1]
    assert report.count("добавлен") == 2 and "не принял ключ" in report


def test_key_prompt_waits_for_a_key_and_explains_mistakes(tmp_path):
    h = Harness(tmp_path)
    h.tap(4, "ky:add", on_keys_button)
    assert h.user_data[4]["awaiting"] == "key"
    assert "Не нашёл ключа" in h.text(4, "привет").replies[-1]["text"]
    h.text(4, KEY)
    assert h.services.keys.values(4) == [KEY]
    assert "awaiting" not in h.user_data[4]


def test_keys_menu_deletes_one_and_clears_after_confirmation(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(4, KEY)
    h.services.keys.add(4, KEY2)
    first = h.services.keys.list(4)[0]
    h.tap(4, f"ky:del:{first.id}", on_keys_button)
    assert h.services.keys.values(4) == [KEY2]

    query = h.tap(4, "ky:clear", on_keys_button)
    assert "ky:clear:yes" in _data(query.edited[-1][1])
    assert h.services.keys.values(4) == [KEY2]
    h.tap(4, "ky:clear:yes", on_keys_button)
    assert h.services.keys.values(4) == []


def test_forgetkey_removes_all_keys(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(4, KEY)
    h.run(on_forgetkey, 4, message=FakeMessage(text="/forgetkey"))
    assert h.services.keys.values(4) == []


# --- jobs -------------------------------------------------------------------------------


def _document(body: bytes, name: str, mime: str, *, on_disk=None, size=None):
    """A Telegram document. With on_disk, the file is where a local Bot API server would have put it."""
    async def download():
        return bytearray(body)

    async def get_file(**kwargs):
        if on_disk is not None:
            on_disk.write_bytes(body)
        return SimpleNamespace(download_as_bytearray=download, file_path=str(on_disk) if on_disk else "documents/f.bin")

    return SimpleNamespace(file_name=name, mime_type=mime, file_size=size if size is not None else len(body),
                           get_file=get_file)


def _voice(duration=75):
    return SimpleNamespace(mime_type="audio/ogg", file_size=64, duration=duration,
                           get_file=_document(b"OggS" + b"\x00" * 60, "v", "audio/ogg").get_file)


def _two_recordings():
    aac = _document(b"\xff\xf1\x50\x80" + b"\x00" * 200, "part2.aac", "audio/aac")
    return [FakeMessage(voice=_voice(), message_id=10), FakeMessage(document=aac, message_id=11)]


def _question(h, user_id, messages):
    h.send_together(user_id, messages)
    asked = [reply for message in messages for reply in message.replies if "Пришло файлов" in reply.get("text", "")]
    assert len(asked) == 1, [m.replies for m in messages]
    return asked[0]


def test_files_sent_together_bring_one_question(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    question = _question(h, 5, _two_recordings())
    assert "1. Голосовое, 1:15" in question["text"] and "2. part2.aac" in question["text"]
    assert not h.calls  # nothing is transcribed before the answer
    data = [button.callback_data for row in question["reply_markup"].inline_keyboard for button in row]
    assert [item.split(":")[1] for item in data] == ["merge", "split", "raw", "cancel"]


def _answer(h, user_id, question, action):
    data = next(button.callback_data for row in question["reply_markup"].inline_keyboard for button in row
                if button.callback_data.startswith(f"bt:{action}:"))
    query = FakeQuery(data, user_id)
    message = FakeMessage(message_id=99)
    query.message = message
    asyncio.run(h._through_gate(on_batch_button, user_id, query=query, message=message))
    return query, message


def test_merge_makes_one_note_from_all_parts_in_order(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    h.services.store.save(5, UserSettings())
    question = _question(h, 5, _two_recordings())
    _, message = _answer(h, 5, question, "merge")
    assert [call[2] for call in h.calls if call[0] == "transcribe"] == ["audio.ogg", "audio.aac"]
    assert len([call for call in h.calls if call[0] == "write"]) == 1
    files = [reply["filename"] for reply in message.replies if "document" in reply]
    assert files == ["Расшифровка.txt", "Лекция.pdf"]
    transcript = next(reply["document"] for reply in message.replies if "document" in reply and "Расшифровка" in reply["filename"])
    assert transcript.decode().startswith("[Часть 1 из 2]")
    assert any("1 из 2" in text for text in message.edits)


def test_split_makes_a_note_per_file(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    question = _question(h, 5, _two_recordings())
    _, message = _answer(h, 5, question, "split")
    assert len([call for call in h.calls if call[0] == "write"]) == 2
    files = [reply["filename"] for reply in message.replies if "document" in reply]
    assert sorted(name.endswith(".pdf") for name in files) == [False, False, True, True]  # a transcript and a PDF each


def test_cancel_and_stale_buttons(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    question = _question(h, 5, _two_recordings())
    query, _ = _answer(h, 5, question, "cancel")
    assert "Отменено" in query.edited[-1][0]
    query, _ = _answer(h, 5, question, "merge")
    assert query.answers == ["Эти файлы уже не ждут ответа"]
    assert not h.calls


def test_files_sent_while_the_question_is_open_join_it(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    _question(h, 5, _two_recordings())
    late = FakeMessage(document=_document(LONG_TEXT.encode(), "notes.txt", "text/plain"), message_id=12)
    h.run(on_media, 5, message=late)
    assert "Пришло файлов: 3" in late.replies[-1]["text"] and "3. notes.txt" in late.replies[-1]["text"]


def test_transcript_without_a_key_asks_for_one(tmp_path):
    h = Harness(tmp_path)
    message = h.text(2, LONG_TEXT)
    assert "нужен ключ Gemini" in message.replies[-1]["text"]
    assert not message.edits


def test_text_document_becomes_pdf(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    message = FakeMessage(document=_document(LONG_TEXT.encode(), "lecture.txt", "text/plain"))
    h.run(on_media, 5, message=message)
    pdf = next(reply for reply in message.replies if "document" in reply)
    assert pdf["filename"] == "Лекция.pdf" and pdf["document"].startswith(b"%PDF")
    assert "Конспект готов" in message.edits[-1]
    assert h.services.state.stats().notes_total == 1


def test_aac_document_with_a_wrong_name_is_sent_as_aac(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    h.services.store.save(5, UserSettings(output="tg"))
    body = b"\xff\xf1\x50\x80" + b"\x00" * 200
    message = FakeMessage(document=_document(body, "recording", "application/octet-stream"))
    h.run(on_media, 5, message=message)
    assert ("transcribe", KEY, "audio.aac") in h.calls
    transcript = next(reply for reply in message.replies if "document" in reply)
    assert transcript["filename"].startswith("Расшифровка")


def test_second_key_takes_over_when_the_first_is_out_of_quota(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, QUOTA_KEY)
    h.services.keys.add(5, KEY)
    message = FakeMessage(voice=_voice())
    h.run(on_media, 5, message=message)
    assert [call[1] for call in h.calls if call[0] == "transcribe"] == [QUOTA_KEY, KEY]
    assert "Конспект готов" in message.edits[-1]
    assert h.services.ring.resting(QUOTA_KEY) > 0


def test_failures_are_reported_and_logged(tmp_path):
    h = Harness(tmp_path, fail_write=GeminiError("Модель не вернула конспект. Попробуйте ещё раз."))
    h.services.keys.add(2, KEY)
    message = h.text(2, LONG_TEXT)
    assert "Модель не вернула конспект" in message.edits[-1]
    assert h.services.state.stats().failures_day == 1


def test_telegram_messages_go_out_as_markdown_v2(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(3, KEY)
    h.services.store.save(3, UserSettings(output="tg"))
    message = h.text(3, LONG_TEXT)
    sent = [reply for reply in message.replies if reply.get("parse_mode") == "MarkdownV2"]
    assert sent and "tg://emoji" in sent[0]["text"]  # premium icons are on by default


def test_shared_keys_serve_users_without_their_own_only_when_enabled(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(POOL, KEY2)
    assert "нужен ключ Gemini" in h.text(6, LONG_TEXT).replies[-1]["text"]
    h.services.state.set_option("shared_keys", True)
    message = h.text(6, LONG_TEXT)
    assert "на общем ключе" in message.edits[-1]
    assert ("write", KEY2, "auto") in h.calls


def test_daily_limit_stops_users_but_not_admins(tmp_path):
    h = Harness(tmp_path)
    h.services.state.set_option("daily_limit", 3)
    for user_id in (7, ADMIN):
        h.services.keys.add(user_id, KEY)
        for _ in range(3):
            h.services.state.log_job(user_id, "text", True, 1.0)
    assert "Лимит" in h.text(7, LONG_TEXT).replies[-1]["text"]
    assert "Конспект готов" in h.text(ADMIN, LONG_TEXT).edits[-1]


# --- access -----------------------------------------------------------------------------


def test_whitelist_blocks_strangers_and_shows_their_id(tmp_path):
    h = Harness(tmp_path, allowed=frozenset({100}))
    message = h.text(999, LONG_TEXT)
    assert "Доступ закрыт" in message.replies[-1]["text"] and "999" in message.replies[-1]["text"]
    assert h.services.state.user(999) is not None  # still recorded, so an admin can find them


def test_banned_users_and_maintenance(tmp_path):
    h = Harness(tmp_path)
    h.services.state.set_banned(8, True)
    assert "Доступ к боту закрыт" in h.text(8, "привет").replies[-1]["text"]
    h.services.state.set_option("maintenance", True)
    assert "обслуживании" in h.text(9, "привет").replies[-1]["text"]
    assert "Настройки" in h.text(ADMIN, "Настройки").replies[-1]["text"]


# --- admin ------------------------------------------------------------------------------


def test_admin_panel_is_closed_to_others_and_explains_how_to_get_in(tmp_path):
    h = Harness(tmp_path)
    message = FakeMessage(text="/admin")
    h.run(admin.on_admin, 1, message=message)
    assert "только админам" in message.replies[-1]["text"]
    query = h.tap(1, "ad:home", admin.on_admin_button)
    assert query.answers == ["Только для админов"]

    assert "ADMIN_IDS=1" in admin.not_admin_text(1, has_admins=False)


def test_admin_home_stats_and_users(tmp_path):
    h = Harness(tmp_path)
    h.text(1, "привет")
    message = FakeMessage(text="/admin")
    h.run(admin.on_admin, ADMIN, message=message)
    assert "Админ-панель" in message.replies[-1]["text"]
    for data in ("ad:stats", "ad:users:0", "ad:user:1", "ad:access", "ad:model", "ad:pool:show", "ad:opt"):
        query = h.tap(ADMIN, data, admin.on_admin_button)
        text, markup = query.edited[-1]
        assert text and markup is not None, data
        assert all(len(item.encode()) <= 64 for item in _data(markup) if item), data


def test_admin_bans_and_promotes_users_but_not_env_admins(tmp_path):
    h = Harness(tmp_path)
    h.tap(ADMIN, "ad:ban:5:1", admin.on_admin_button)
    assert h.services.state.is_banned(5)
    h.tap(ADMIN, "ad:adm:5:1", admin.on_admin_button)
    assert h.services.state.is_admin(5)
    query = h.tap(5, f"ad:ban:{ADMIN}:1", admin.on_admin_button)
    assert not h.services.state.is_banned(ADMIN)
    assert query.answers[-1] == "Этого пользователя так не изменить"


def test_admin_switches_access_and_adds_ids(tmp_path):
    h = Harness(tmp_path)
    h.tap(ADMIN, "ad:access:whitelist", admin.on_admin_button)
    assert h.services.options().access == "whitelist"
    h.tap(ADMIN, "ad:wladd", admin.on_admin_button)
    h.text(ADMIN, "11, 12")
    assert h.services.state.whitelist() == [11, 12]
    assert "Доступ закрыт" not in h.text(11, "Помощь").replies[-1]["text"]


def test_admin_model_choice_is_checked_before_saving(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(ADMIN, KEY)
    h.tap(ADMIN, "ad:model:set:1", admin.on_admin_button)
    assert h.services.options().model == admin.MODEL_PRESETS[1]

    h.tap(ADMIN, "ad:model:custom", admin.on_admin_button)
    message = h.text(ADMIN, "broken-model")
    assert "не ответила" in message.edits[-1]
    assert h.services.options().model == admin.MODEL_PRESETS[1]
    h.tap(ADMIN, "ad:model:custom", admin.on_admin_button)
    h.text(ADMIN, "models/gemini-new")
    assert h.services.options().model == "gemini-new"


def test_admin_shared_keys_and_options(tmp_path):
    h = Harness(tmp_path)
    h.tap(ADMIN, "ad:pool:add", admin.on_admin_button)
    h.text(ADMIN, f"{KEY} {KEY2}")
    assert h.services.keys.values(POOL) == [KEY, KEY2]
    h.tap(ADMIN, "ad:pool:toggle", admin.on_admin_button)
    assert h.services.options().shared_keys

    h.tap(ADMIN, "ad:opt:premium_emoji", admin.on_admin_button)
    assert h.services.options().premium_emoji is False
    query = h.tap(ADMIN, "ad:opt:daily_limit", admin.on_admin_button)
    assert h.services.options().daily_limit == 3
    assert "<tg-emoji" not in query.edited[-1][0]  # premium off means plain emoji


def test_broadcast_copies_the_draft_to_everyone_not_banned(tmp_path):
    h = Harness(tmp_path)
    for user_id in (1, 2, 3):
        h.text(user_id, "привет")
    h.services.state.set_banned(3, True)
    h.tap(ADMIN, "ad:bc", admin.on_admin_button)
    draft = FakeMessage(text="Новости бота", message_id=42)
    h.run(on_text, ADMIN, message=draft)
    assert "уйдёт" in draft.replies[-1]["text"]
    h.tap(ADMIN, "ad:bc:send", admin.on_admin_button)
    assert sorted(h.bot.copied) == [1, 2, ADMIN]
    assert "Доставлено: 3" in h.bot.sent[-1][1]


@pytest.mark.parametrize("notify", [True, False])
def test_admins_hear_about_new_users_when_asked(tmp_path, notify):
    h = Harness(tmp_path)
    h.services.state.set_option("notify_new_users", notify)
    h.text(77, "привет")
    h.text(77, "ещё раз")
    assert len([chat for chat, _ in h.bot.sent if chat == ADMIN]) == (1 if notify else 0)


def test_second_note_waits_in_line_instead_of_being_refused(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(3, KEY)
    first, second = FakeMessage(text=LONG_TEXT, message_id=1), FakeMessage(text=LONG_TEXT, message_id=2)

    async def both():
        await asyncio.gather(
            h._through_gate(on_text, 3, message=first),
            h._through_gate(on_text, 3, message=second),
        )

    asyncio.run(both())
    assert "Предыдущий" not in "".join(second.texts())
    assert any("В очереди: перед вами 1" in text for text in second.texts())
    assert "Конспект готов" in second.edits[-1] and "Конспект готов" in first.edits[-1]
    assert len([call for call in h.calls if call[0] == "write"]) == 2
    assert h.services.queued == {}


def test_queue_has_a_limit_per_user(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "MAX_QUEUED_PER_USER", 1)
    h = Harness(tmp_path)
    h.services.keys.add(3, KEY)
    h.services.queued[3] = 1
    message = h.text(3, LONG_TEXT)
    assert "уже в очереди 1 конспект" in message.replies[-1]["text"]
    assert not h.calls


@pytest.mark.parametrize(("count", "word"), [(1, "конспект"), (2, "конспекта"), (4, "конспекта"), (5, "конспектов"),
                                             (11, "конспектов"), (21, "конспект")])
def test_notes_are_counted_in_russian(count, word):
    assert bot._notes(count) == f"{count} {word}"


def test_files_over_20_mb_are_refused_by_the_cloud_api_with_a_hint(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    big = _document(b"OggS" + b"\x00" * 60, "lecture.ogg", "audio/ogg", size=25 * 1024 * 1024)
    message = FakeMessage(document=big)
    h.run(on_media, 5, message=message)
    assert "больше 20 МБ" in message.replies[-1]["text"] and "Сожмите" in message.replies[-1]["text"]
    assert not h.calls


def _local_harness(tmp_path):
    h = Harness(tmp_path)
    h.services.config = Config(telegram_token="t", gemini_model="m", data_dir=tmp_path, allowed_user_ids=frozenset(),
                               admin_ids=frozenset({ADMIN}), telegram_api_url="http://127.0.0.1:8081")
    h.services.keys.add(5, KEY)
    return h


def test_local_server_lifts_the_limit_and_files_are_read_from_disk(tmp_path):
    h = _local_harness(tmp_path)
    on_disk = tmp_path / "file_1.ogg"
    big = _document(b"OggS" + b"\x00" * 60, "lecture.ogg", "audio/ogg", on_disk=on_disk, size=90 * 1024 * 1024)
    message = FakeMessage(document=big)
    h.run(on_media, 5, message=message)
    assert ("transcribe", KEY, "audio.ogg") in h.calls
    assert "Конспект готов" in message.edits[-1]


def test_local_server_still_has_an_upper_bound(tmp_path):
    h = _local_harness(tmp_path)
    message = FakeMessage(document=_document(b"x", "huge.wav", "audio/wav", size=3000 * 1024 * 1024))
    h.run(on_media, 5, message=message)
    assert "больше 2000 МБ" in message.replies[-1]["text"] and "Разбейте" in message.replies[-1]["text"]


def test_three_big_parts_merge_into_one_note_without_loading_them(tmp_path):
    h = _local_harness(tmp_path)
    parts = []
    for number in range(3):
        document = _document(b"OggS" + b"\x00" * 60, f"part{number}.ogg", "audio/ogg", on_disk=tmp_path / f"f{number}.ogg",
                             size=80 * 1024 * 1024)
        parts.append(FakeMessage(document=document, message_id=20 + number))
    question = _question(h, 5, parts)
    assert "Пришло файлов: 3" in question["text"]
    _answer(h, 5, question, "merge")
    assert [call[0] for call in h.calls].count("transcribe") == 3
    assert [call[0] for call in h.calls].count("write") == 1


def test_both_formats_arrive_automatically_messages_first(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(3, KEY)
    message = h.text(3, LONG_TEXT)
    kinds = ["document" if "document" in reply else "text" for reply in message.replies if "document" in reply or reply.get("parse_mode")]
    first_pdf = kinds.index("document")
    assert first_pdf > 0 and set(kinds[:first_pdf]) == {"text"}  # the messages come before the PDF
    assert [r for r in message.replies if "document" in r][0]["filename"] == "Лекция.pdf"


def test_admin_update_button_leaves_a_request_for_the_server(tmp_path):
    h = Harness(tmp_path)
    query = h.tap(ADMIN, "ad:update", admin.on_admin_button)
    assert (tmp_path / "update.request").read_text() == str(ADMIN)
    assert "Обновление запрошено" in query.edited[-1][0]


def test_only_admins_can_request_an_update(tmp_path):
    h = Harness(tmp_path)
    query = h.tap(1, "ad:update", admin.on_admin_button)
    assert query.answers == ["Только для админов"]
    assert not (tmp_path / "update.request").exists()


def test_the_updater_is_told_how_many_notes_are_running(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(3, KEY)
    seen = []
    original = h.services.make_client

    def make_client(api_key, model):
        client = original(api_key, model)
        write = client.write

        async def watching_write(*args, **kwargs):
            seen.append((tmp_path / "active.jobs").read_text())
            return await write(*args, **kwargs)

        client.write = watching_write
        return client

    h.services.make_client = make_client
    h.text(3, LONG_TEXT)
    assert seen == ["1"]
    assert (tmp_path / "active.jobs").read_text() == "0"


def test_transcript_file_arrives_even_when_writing_the_note_fails(tmp_path):
    h = Harness(tmp_path, fail_write=GeminiError("Конспект получился в неверном формате. Попробуйте ещё раз."))
    h.services.keys.add(5, KEY)
    question = _question(h, 5, _two_recordings())
    _, message = _answer(h, 5, question, "merge")
    assert "неверном формате" in message.edits[-1]
    files = [reply["filename"] for reply in message.replies if "document" in reply]
    assert files == ["Расшифровка.txt"]


def test_raw_button_sends_only_the_transcript(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    question = _question(h, 5, _two_recordings())
    _, message = _answer(h, 5, question, "raw")
    assert [call for call in h.calls if call[0] == "write"] == []
    files = [reply["filename"] for reply in message.replies if "document" in reply]
    assert files == ["Расшифровка.txt"]
    assert "Расшифровка готова" in message.edits[-1]


def test_single_recording_is_only_asked_about_when_the_setting_is_on(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    h.tap(5, "st:raw", on_settings_button)
    assert h.services.store.get(5).ask_raw is True
    question = _question(h, 5, _two_recordings()[:1])
    assert "Только расшифровка" in str(question["reply_markup"])
    assert [call for call in h.calls if call[0] == "write"] == []
    h.tap(5, "st:raw", on_settings_button)
    assert h.services.store.get(5).ask_raw is False


def _pdf(text: str) -> bytes:
    from weasyprint import HTML

    return HTML(string=f"<p>{text}</p>").write_pdf()


def test_pdf_with_text_becomes_the_lecture(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    body = _pdf(LONG_TEXT)
    message = FakeMessage(document=_document(body, "lecture.pdf", "application/pdf"))
    h.run(on_media, 5, message=message)
    assert ("write", KEY, "auto") in h.calls
    assert "Конспект готов" in message.edits[-1]


def test_scanned_pdf_without_text_is_refused(tmp_path):
    from weasyprint import HTML

    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    body = HTML(string="<p></p>").write_pdf()
    message = FakeMessage(document=_document(body, "scan.pdf", "application/pdf"))
    h.run(on_media, 5, message=message)
    assert "похоже, это скан" in message.replies[-1]["text"]
    assert [call for call in h.calls if call[0] == "write"] == []


def test_extras_can_be_switched_on_in_the_settings_menu(tmp_path):
    h = Harness(tmp_path)
    h.tap(1, "st:ext:questions", on_settings_button)
    assert h.services.store.get(1).extras == ("questions",)
    h.tap(1, "st:ext:questions", on_settings_button)
    assert h.services.store.get(1).extras == ()


def test_nothing_is_made_before_the_agreement_is_accepted(tmp_path):
    from notes_bot.agreement import AGREEMENT_VERSION

    h = Harness(tmp_path)
    h.auto_accept = False
    h.services.keys.add(8, KEY)
    message = h.text(8, LONG_TEXT)
    assert not [call for call in h.calls if call[0] == "write"]
    assert "Пользовательское соглашение" in message.replies[-1]["text"]
    assert h.services.state.agreed_version(8) is None
    assert AGREEMENT_VERSION in str(message.replies[-1]["reply_markup"])


def test_accepting_the_agreement_lets_the_user_make_notes(tmp_path):
    from notes_bot.agreement import AGREEMENT_VERSION
    from notes_bot.bot import on_agreement_button

    h = Harness(tmp_path)
    h.auto_accept = False
    h.services.keys.add(8, KEY)
    query = FakeQuery(f"ag:yes:{AGREEMENT_VERSION}", 8)
    h.run(on_agreement_button, 8, query=query)
    assert h.services.state.agreed_version(8) == AGREEMENT_VERSION
    message = h.text(8, LONG_TEXT)
    assert "Конспект готов" in message.edits[-1]


def test_an_old_agreement_version_does_not_count(tmp_path):
    from notes_bot.agreement import AGREEMENT_VERSION
    from notes_bot.bot import on_agreement_button

    h = Harness(tmp_path)
    h.auto_accept = False
    h.services.keys.add(8, KEY)
    h.services.state.accept_agreement(8, "old-version")
    message = h.text(8, LONG_TEXT)
    assert "Пользовательское соглашение" in message.replies[-1]["text"]
    query = FakeQuery("ag:yes:old-version", 8)
    h.run(on_agreement_button, 8, query=query)
    assert "Соглашение изменилось" in query.answers[-1]
    assert h.services.state.agreed_version(8) == "old-version"  # a stale tap changes nothing


# --- group topics -----------------------------------------------------------------------


def _in_group(text, thread_id=None, chat_id=-1001234):
    message = FakeMessage(text=text)
    message.chat = SimpleNamespace(id=chat_id, type="supergroup")
    message.message_thread_id = thread_id
    return message


def _bind(h, user_id, subject, thread_id, chat_id=-1001234):
    from notes_bot.bot import on_bind

    message = _in_group(f"/bind {subject}", thread_id, chat_id)
    h.run(on_bind, user_id, message=message)
    return message


def test_bind_remembers_the_topic_for_the_subject(tmp_path):
    h = Harness(tmp_path)
    reply_text = _bind(h, 5, "Физика", 17).replies[-1]["text"]
    assert "привязан" in reply_text
    assert h.services.state.topics(5) == [("Физика", -1001234, 17)]
    assert "внутри самого раздела" in _bind(h, 5, "Химия", None).replies[-1]["text"]
    assert h.services.state.topics(5) == [("Физика", -1001234, 17)]


def test_bind_outside_a_group_is_refused(tmp_path):
    from notes_bot.bot import on_bind

    h = Harness(tmp_path)
    message = FakeMessage(text="/bind Физика")
    message.chat = SimpleNamespace(id=5, type="private")
    message.message_thread_id = None
    h.run(on_bind, 5, message=message)
    assert "нужна в группе" in message.replies[-1]["text"]
    assert h.services.state.topics(5) == []


def test_a_note_is_offered_to_the_matching_topic_first_and_posted_on_a_tap(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    _bind(h, 5, "Химия", 3)
    _bind(h, 5, "Лекция", 4)  # the fake note is titled "Лекция", so this subject matches it
    message = h.text(5, LONG_TEXT)
    offer = next(reply for reply in message.replies if "Отправить этот конспект" in reply.get("text", ""))
    labels = [button.text for row in offer["reply_markup"].inline_keyboard for button in row]
    assert labels[0] == "✅ Лекция" and labels[1] == "Химия"
    token = offer["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]

    h.tap(5, f"tp:{token}:0", on_topic_button)
    assert h.bot.topic_messages and set(h.bot.topic_messages) == {(-1001234, 4)}
    assert h.bot.documents == [(-1001234, 4, "Лекция.pdf")]


def test_a_note_is_not_posted_without_a_tap_and_cancel_keeps_it_home(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    _bind(h, 5, "Физика", 4)
    message = h.text(5, LONG_TEXT)
    assert not h.bot.topic_messages and not h.bot.documents
    offer = next(reply for reply in message.replies if "Отправить этот конспект" in reply.get("text", ""))
    token = offer["reply_markup"].inline_keyboard[-1][0].callback_data.split(":")[1]
    h.tap(5, f"tp:{token}:x", on_topic_button)
    assert not h.bot.topic_messages and not h.bot.documents


def test_no_offer_without_bound_topics(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    message = h.text(5, LONG_TEXT)
    assert not any("Отправить этот конспект" in reply.get("text", "") for reply in message.replies)


def test_another_user_cannot_post_someone_elses_note(tmp_path):
    h = Harness(tmp_path)
    h.services.keys.add(5, KEY)
    _bind(h, 5, "Лекция", 4)
    message = h.text(5, LONG_TEXT)
    offer = next(reply for reply in message.replies if "Отправить этот конспект" in reply.get("text", ""))
    token = offer["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]
    h.tap(6, f"tp:{token}:0", on_topic_button)  # user 6 tries to tap user 5's button
    assert not h.bot.topic_messages and not h.bot.documents
    assert h.services.posts  # the offer is still waiting for its owner
    h.tap(5, f"tp:{token}:0", on_topic_button)
    assert h.bot.documents == [(-1001234, 4, "Лекция.pdf")]


def test_each_user_sees_only_their_own_topics(tmp_path):
    h = Harness(tmp_path)
    _bind(h, 5, "Физика", 10, chat_id=-100)
    _bind(h, 6, "Физика", 20, chat_id=-200)
    assert h.services.state.topics(5) == [("Физика", -100, 10)]
    assert h.services.state.topics(6) == [("Физика", -200, 20)]
