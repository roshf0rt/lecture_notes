"""Exercise document improvement through the existing bot's real handlers.

Telegram transport and lecture jobs are simulated; DOCX processing is real.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

from docx import Document
from lxml import etree
import pytest
from telegram.ext import ApplicationHandlerStop

from notes_bot import admin, bot, docx_workflow
from notes_bot.agreement import AGREEMENT_VERSION
from notes_bot.config import Config
from notes_bot.docx_cleaner import APP, MAX_INPUT
from notes_bot.keys import KeyVault
from notes_bot.services import Services
from notes_bot.settings import SettingsStore
from notes_bot.storage import BotState, Options


class Message:
    def __init__(self, *, text=None, document=None, caption=None, reply_to=None):
        self.text = text
        self.document = document
        self.caption = caption
        self.reply_to_message = reply_to
        self.voice = self.audio = self.video = self.video_note = self.animation = None
        self.photo = None
        self.chat_id = 1
        self.message_id = 10
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return SimpleNamespace(edit_text=AsyncMock())

    async def reply_document(self, document, filename=None, **kwargs):
        content = document.getvalue() if hasattr(document, "getvalue") else document.read()
        self.replies.append({"document": content, "filename": filename, **kwargs})


def document_bytes():
    document = Document()
    document.core_properties.author = "Previous author"
    document.add_paragraph("Текст\u200b документа\u00a0для улучшения. " * 8)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


class Harness:
    def __init__(self, folder: Path, *, accepted=True):
        self.data = document_bytes()
        self.attachment = SimpleNamespace(
            file_id="docx-fixture", file_name="lecture.docx", file_size=len(self.data),
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.context = SimpleNamespace(user_data={})
        self.accepted = accepted
        self.model_calls = []

        def client(*args):
            self.model_calls.append(args)
            raise AssertionError("Document improvement must not ask a model to process the file")

        config = Config(telegram_token="test-token", gemini_model="test-model", data_dir=folder,
                        allowed_user_ids=frozenset())
        self.services = Services(
            config=config, store=SettingsStore(folder / "settings.sqlite3"),
            keys=KeyVault(folder), state=BotState(folder / "state.sqlite3", Options(model="test-model", premium_emoji=False)),
            make_client=client,
        )

        async def download(**kwargs):
            return bytearray(self.data)

        self.remote = SimpleNamespace(file_path=None, download_as_bytearray=AsyncMock(side_effect=download))
        self.context.bot = SimpleNamespace(username="lecture_bot", get_file=AsyncMock(return_value=self.remote))
        self.tasks = []
        self.context.application = SimpleNamespace(
            bot_data={"services": self.services}, create_task=self.tasks.append,
        )

    def update(self, message, *, chat_type="private"):
        return SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=1, type=chat_type),
            effective_user=SimpleNamespace(id=1, username="student", full_name="Student"),
            callback_query=None,
        )

    async def call(self, handler, message, *, chat_type="private"):
        if self.accepted:
            self.services.state.accept_agreement(1, AGREEMENT_VERSION)
        update = self.update(message, chat_type=chat_type)
        try:
            await bot.gate(update, self.context)
        except ApplicationHandlerStop:
            return
        await handler(update, self.context)
        while self.tasks:
            await self.tasks.pop(0)

    def send(self, handler, message, **kwargs):
        asyncio.run(self.call(handler, message, **kwargs))
        return message

    def output(self, message):
        replies = [reply for reply in message.replies if "document" in reply]
        assert len(replies) == 1
        assert replies[0]["filename"] == "lecture.docx"
        result = Document(BytesIO(replies[0]["document"]))
        assert "\u200b" not in result.paragraphs[0].text
        assert "\u00a0" not in result.paragraphs[0].text
        assert not self.model_calls
        assert not self.services.queued
        assert (self.services.config.data_dir / "active.jobs").read_text() == "0"
        return result


def test_caption_improves_docx_without_model_key(tmp_path):
    h = Harness(tmp_path)
    assert h.services.keys.count(1) == 0
    message = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Иван Иванов, Анна Петрова"))
    result = h.output(message)
    assert result.core_properties.author == "Иван Иванов; Анна Петрова"
    assert result.core_properties.last_modified_by == "Анна Петрова"
    assert result.core_properties.created < result.core_properties.modified


def test_authors_only_automatically_fill_time_and_application(tmp_path):
    h = Harness(tmp_path)
    source = Document()
    source.add_paragraph('А' * 14950)
    buffer = BytesIO()
    source.save(buffer)
    h.data = buffer.getvalue()
    h.attachment.file_size = len(h.data)
    message = h.send(bot.on_media, Message(document=h.attachment, caption='/clean Перескоков Иван'))
    result = h.output(message)
    assert result.core_properties.author == 'Перескоков Иван'
    assert result.core_properties.created == source.core_properties.created
    assert len([reply for reply in message.replies if 'text' in reply]) == 1
    upload = next(reply for reply in message.replies if 'document' in reply)
    with ZipFile(BytesIO(upload['document'])) as z:
        app = etree.fromstring(z.read('docProps/app.xml'))
        assert app.find(f'{{{APP}}}TotalTime').text == '90'
        assert app.find(f'{{{APP}}}Application').text == 'Microsoft Office Word'
    assert 'оценка подготовки текста' in upload['caption'].lower()


@pytest.mark.parametrize('suffix, expected_minutes, expected_application', [
    (' | 15 | Microsoft Office Word', 15, 'Microsoft Office Word'),
    (' | 0', 0, 'Microsoft Office Word'),
    (' | | LibreOffice Writer', None, 'LibreOffice Writer'),
])
def test_caption_can_set_editing_properties_without_fabricating_creation(tmp_path, suffix,
                                                                        expected_minutes, expected_application):
    h = Harness(tmp_path)
    source_created = Document(BytesIO(h.data)).core_properties.created
    message = h.send(bot.on_media, Message(document=h.attachment, caption='/clean Перескоков Иван' + suffix))
    result = h.output(message)
    assert result.core_properties.author == 'Перескоков Иван'
    assert result.core_properties.created == source_created
    upload = next(reply for reply in message.replies if 'document' in reply)
    with ZipFile(BytesIO(upload['document'])) as z:
        app = etree.fromstring(z.read('docProps/app.xml'))
        assert app.find(f'{{{APP}}}Application').text == expected_application
        minutes = int(app.find(f'{{{APP}}}TotalTime').text)
        if expected_minutes is None:
            assert minutes == 5
        else:
            assert minutes == expected_minutes
            assert 'указанное время' in upload['caption'].lower()


def test_author_followup_accepts_metadata_options_and_returns_docx(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    lecture = AsyncMock()
    monkeypatch.setattr(bot, '_run_job', lecture)
    h.send(bot.on_media, Message(document=h.attachment))
    message = h.send(bot.on_text, Message(text='Перескоков Иван | 8 | Microsoft Office Word'))
    assert h.output(message).core_properties.author == 'Перескоков Иван'
    upload = next(reply for reply in message.replies if 'document' in reply)
    with ZipFile(BytesIO(upload['document'])) as z:
        app = etree.fromstring(z.read('docProps/app.xml'))
        assert app.find(f'{{{APP}}}TotalTime').text == '8'
        assert app.find(f'{{{APP}}}Application').text == 'Microsoft Office Word'
    lecture.assert_not_awaited()


@pytest.mark.parametrize('options', [
    'Автор | -1', 'Автор | 1.5', 'Автор | вчера', 'Автор | 10081',
    'Автор | 15 | ' + 'x' * 101, 'Автор | 15 | Word\x00test',
    'Автор | 15 | Word | лишнее',
])
def test_invalid_metadata_options_are_reported_before_download(tmp_path, options):
    h = Harness(tmp_path)
    message = h.send(bot.on_media, Message(document=h.attachment, caption='/clean ' + options))
    assert any('text' in reply for reply in message.replies)
    assert not any('document' in reply for reply in message.replies)
    h.context.bot.get_file.assert_not_awaited()


def test_bare_docx_asks_authors_and_followup_is_not_lecture(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    lecture = AsyncMock()
    monkeypatch.setattr(bot, "_run_job", lecture)
    upload = h.send(bot.on_media, Message(document=h.attachment))
    assert any("автор" in reply.get("text", "").lower() for reply in upload.replies)
    h.context.bot.get_file.assert_not_awaited()
    authors = h.send(bot.on_text, Message(text="Иван Иванов, Анна Петрова"))
    assert h.output(authors).core_properties.author == "Иван Иванов; Анна Петрова"
    lecture.assert_not_awaited()


def test_clean_command_reply_uses_existing_document(tmp_path):
    h = Harness(tmp_path)
    h.context.user_data["semester"] = ("Previous subject", time.time())
    message = Message(text="/clean Иван Иванов", reply_to=Message(document=h.attachment))
    h.send(bot.on_clean, message)
    assert h.output(message).core_properties.author == "Иван Иванов"
    assert "semester" not in h.context.user_data


def test_improve_command_and_menu_do_not_need_key(tmp_path):
    from notes_bot.menus import MENU_DOCX

    h = Harness(tmp_path)
    for handler, message in ((bot.on_improve, Message(text="/improve")),
                             (bot.on_text, Message(text=MENU_DOCX))):
        h.send(handler, message)
        assert any("DOCX" in reply.get("text", "") for reply in message.replies)
    assert not h.model_calls
    h.context.bot.get_file.assert_not_awaited()


def test_cancel_removes_pending_document_then_text_is_lecture(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.send(bot.on_media, Message(document=h.attachment))
    h.send(bot.on_cancel, Message(text="/cancel"))
    lecture = AsyncMock()
    monkeypatch.setattr(bot, "_run_job", lecture)
    h.send(bot.on_text, Message(text="Теперь обычный текст лекции. " * 10))
    lecture.assert_awaited_once()
    h.context.bot.get_file.assert_not_awaited()


@pytest.mark.parametrize("command", ["on_start", "on_help", "on_settings", "on_keys"])
def test_navigation_exits_document_author_prompt(tmp_path, monkeypatch, command):
    h = Harness(tmp_path)
    h.send(bot.on_media, Message(document=h.attachment))
    h.send(getattr(bot, command), Message(text="/" + command.removeprefix("on_")))
    lecture = AsyncMock()
    monkeypatch.setattr(bot, "_run_job", lecture)
    h.send(bot.on_text, Message(text="Текст лекции после выхода из обработки документа. " * 5))
    lecture.assert_awaited_once()
    h.context.bot.get_file.assert_not_awaited()


def test_docx_requires_agreement_before_any_download(tmp_path):
    h = Harness(tmp_path, accepted=False)
    message = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Автор"))
    assert any("соглашение" in reply.get("text", "").lower() for reply in message.replies)
    h.context.bot.get_file.assert_not_awaited()
    assert not any("document" in reply for reply in message.replies)


@pytest.mark.parametrize("blocked", ["banned", "maintenance", "whitelist"])
def test_existing_access_gate_applies_to_docx(tmp_path, blocked):
    h = Harness(tmp_path)
    if blocked == "banned":
        h.services.state.set_banned(1, True)
    elif blocked == "maintenance":
        h.services.state.set_option("maintenance", True)
    else:
        h.services.state.set_option("access", "whitelist")
    message = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Автор"))
    h.context.bot.get_file.assert_not_awaited()
    assert not any("document" in reply for reply in message.replies)


def test_bad_authors_size_and_bad_package_are_reported(tmp_path):
    h = Harness(tmp_path)
    invalid_names = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Один,,Два"))
    assert any("text" in reply for reply in invalid_names.replies)
    h.context.bot.get_file.assert_not_awaited()
    h.attachment.file_size = MAX_INPUT + 1
    too_large = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Автор"))
    assert any("20" in reply.get("text", "") for reply in too_large.replies)
    h.context.bot.get_file.assert_not_awaited()
    h.attachment.file_size = len(h.data)
    h.data = b"broken archive"
    malformed = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Автор"))
    assert any("text" in reply for reply in malformed.replies)
    assert not any("document" in reply for reply in malformed.replies)
    assert not h.services.queued


def test_existing_key_color_and_admin_replies_keep_their_routes(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    save_keys, save_color, admin_reply = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(bot, "_save_keys", save_keys)
    monkeypatch.setattr(bot, "_save_color", save_color)
    monkeypatch.setattr(admin, "handle_awaited", admin_reply)
    h.context.user_data["awaiting"] = bot.AWAIT_KEY
    h.send(bot.on_text, Message(text="AIzaSyTestKeyValue1234567890abcdefghij"))
    save_keys.assert_awaited_once()
    h.context.user_data["awaiting"] = bot.AWAIT_COLOR
    h.send(bot.on_text, Message(text="#3B82F6"))
    save_color.assert_awaited_once()
    h.context.user_data["awaiting"] = admin.AWAIT_FIND
    h.send(bot.on_text, Message(text="student"))
    admin_reply.assert_awaited_once()


def test_expired_author_prompt_never_downloads_or_starts_lecture(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.send(bot.on_media, Message(document=h.attachment))
    pending = h.context.user_data[docx_workflow.PENDING]
    h.context.user_data[docx_workflow.PENDING] = replace(
        pending, created=time.time() - docx_workflow.PENDING_TTL - 1,
    )
    lecture = AsyncMock()
    monkeypatch.setattr(bot, "_run_job", lecture)
    answer = h.send(bot.on_text, Message(text="Иван Иванов"))
    assert any("истекло" in reply.get("text", "") for reply in answer.replies)
    assert docx_workflow.PENDING not in h.context.user_data
    assert "awaiting" not in h.context.user_data
    h.context.bot.get_file.assert_not_awaited()
    lecture.assert_not_awaited()


def test_local_bot_api_file_is_read_without_download(tmp_path):
    h = Harness(tmp_path)
    original = tmp_path / "telegram-input.docx"
    original.write_bytes(h.data)
    h.services.config = replace(h.services.config, telegram_api_url="http://local-api:8081")
    h.remote.file_path = str(original)
    result = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Иван Иванов"))
    assert h.output(result).core_properties.author == "Иван Иванов"
    h.remote.download_as_bytearray.assert_not_awaited()
    assert original.read_bytes() == h.data


def test_docx_does_not_intercept_admin_broadcast_attachment(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.context.user_data["awaiting"] = admin.AWAIT_BROADCAST
    broadcast = AsyncMock()
    monkeypatch.setattr(admin, "handle_awaited", broadcast)
    h.send(bot.on_media, Message(document=h.attachment, caption="/clean Автор"))
    broadcast.assert_awaited_once()
    h.context.bot.get_file.assert_not_awaited()


def test_docx_in_group_is_not_processed(tmp_path):
    h = Harness(tmp_path)
    result = h.send(bot.on_media, Message(document=h.attachment, caption="/clean Автор"), chat_type="supergroup")
    h.context.bot.get_file.assert_not_awaited()
    assert not any("document" in reply for reply in result.replies)
    assert docx_workflow.PENDING not in h.context.user_data


def test_cancellation_keeps_job_active_until_background_processing_finishes(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    actual_processor = docx_workflow.clean_docx
    release = threading.Event()

    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def processor(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=3):
                raise AssertionError("Test did not release the processing thread")
            return actual_processor(*args, **kwargs)

        monkeypatch.setattr(docx_workflow, "clean_docx", processor)
        message = Message(document=h.attachment, caption="/clean Автор")
        task = asyncio.create_task(h.call(bot.on_media, message))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert h.services.queued == {1: 1}
            assert (tmp_path / "active.jobs").read_text() == "1"
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert (tmp_path / "active.jobs").read_text() == "1"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not h.services.queued
        assert (tmp_path / "active.jobs").read_text() == "0"
        assert not any("document" in reply for reply in message.replies)

    asyncio.run(scenario())
