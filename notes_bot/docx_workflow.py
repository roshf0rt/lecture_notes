"""Local DOCX improvement inside the lecture bot's existing Telegram workflow."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from telegram import Update
from telegram.ext import ContextTypes

from .docx_cleaner import MAX_INPUT, InvalidDocument, clean_docx
from .services import reply, services
from .ui import html

log = logging.getLogger(__name__)

AWAIT_FILE = "docx:file"
AWAIT_AUTHORS = "docx:authors"
PENDING = "docx_pending"
PENDING_TTL = 900
MAX_QUEUED_PER_USER = 5
FILE_TIMEOUT = 120
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
COMMAND = re.compile(
    r"^/(?:clean|improve|docx)(?:@(?P<target>[A-Za-z0-9_]+))?(?:\s+(?P<authors>.*))?$",
    re.IGNORECASE | re.DOTALL,
)

INSTRUCTIONS = (
    "<b>Улучшение DOCX</b>\n\n"
    "Пришлите документ Word <code>.docx</code> — я удалю технический мусор и верну обработанную копию. "
    "Имена авторов запрошу отдельно. Чтобы сделать всё одним сообщением, добавьте к файлу подпись:\n"
    "<code>/clean Иван Иванов, Анна Петрова</code>\n\n"
    "При необходимости укажите время в минутах и значение свойства Application через вертикальную черту:\n"
    "<code>/clean Иван Иванов | 15 | Microsoft Office Word</code>\n"
    "Эти два значения необязательны. Application — редактируемая подпись приложения, "
    "она не подтверждает сохранение в Word.\n\n"
    "Обработка удаляет комментарии и скрытые фрагменты, принимает поддерживаемые исправления, "
    "упорядочивает свойства и сохраняет тире, кавычки и многоточия. Сохраните оригинал и проверьте результат в Word. "
    "Без указанного времени используется оценка проверки текста: 1000 знаков в минуту, от 1 до 120 минут. "
    "Исходная корректная дата создания сохраняется; дата изменения — момент обработки. "
    "Файл обрабатывается на сервере бота, без обращения к сервисам создания конспектов; ключ не нужен. "
    "Лимит DOCX — 20 МБ. Отмена: /cancel."
)
AUTHOR_PROMPT = (
    "Укажите автора или нескольких авторов через запятую, одной строкой (до 200 символов). "
    "Можно дополнительно указать минуты и Application: <code>Иван Иванов | 15 | Microsoft Office Word</code>. "
    "Оба дополнительных значения необязательны. Имена будут записаны в свойства DOCX. "
    "Без введённых минут время будет оценкой проверки текста, а не сведениями о реальной работе. "
    "Application не подтверждает сохранение в соответствующем приложении. Комментарии, скрытые фрагменты и история поддерживаемых "
    "исправлений будут удалены. Для отмены: /cancel."
)


@dataclass(frozen=True)
class PendingDocument:
    file_id: str
    name: str
    created: float


@dataclass(frozen=True)
class DocumentRequest:
    authors: list[str]
    editing_minutes: int | None = None
    application_name: str = "DOCX Improver"


def is_document(message) -> bool:
    document = getattr(message, "document", None)
    return document is not None and (
        (document.file_name or "").lower().endswith(".docx")
        or (document.mime_type or "").lower() == DOCX_MIME
    )


def is_waiting(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("awaiting") in (AWAIT_FILE, AWAIT_AUTHORS)


def clear_pending(context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.pop(PENDING, None)
    waiting = is_waiting(context)
    if waiting:
        context.user_data.pop("awaiting", None)
    return pending is not None or waiting


def parse_authors(text: str) -> list[str]:
    value = text.strip()
    if (not value or len(value) > 200 or
            any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF or ord(ch) in (0xFFFE, 0xFFFF) for ch in value)):
        raise InvalidDocument("Введите имена одной строкой, не более 200 символов, или отправьте /cancel.")
    names = [name.strip() for name in value.split(",")]
    if any(not name for name in names) or len("; ".join(names)) > 200:
        raise InvalidDocument("Укажите непустые имена через запятую, не более 200 символов в сумме, или /cancel.")
    return names


def parse_request(text: str) -> DocumentRequest:
    """Parse authors and optional editable properties without inferring provenance."""
    pieces = text.split("|")
    if len(pieces) > 3:
        raise InvalidDocument("Используйте формат: Авторы | минуты | Application. Последние два поля необязательны.")
    authors = parse_authors(pieces[0])
    minutes = None
    if len(pieces) >= 2 and pieces[1].strip():
        value = pieces[1].strip()
        if len(value) > 5 or not value.isascii() or not value.isdecimal() or not 0 <= int(value) <= 10080:
            raise InvalidDocument("Время редактирования должно быть целым числом от 0 до 10080 минут.")
        minutes = int(value)
    application = pieces[2].strip() if len(pieces) == 3 and pieces[2].strip() else "DOCX Improver"
    if (len(application) > 100 or any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF or
                                    ord(ch) in (0xFFFE, 0xFFFF) for ch in application)):
        raise InvalidDocument("Application должно быть одной строкой длиной до 100 символов без управляющих знаков.")
    return DocumentRequest(authors, minutes, application)


def _pending(document) -> PendingDocument:
    if document is None or not (
        (document.file_name or "").lower().endswith(".docx")
        or (document.mime_type or "").lower() == DOCX_MIME
    ):
        raise InvalidDocument("Для улучшения нужен файл .docx без пароля и макросов. Отмена: /cancel.")
    if document.file_size and document.file_size > MAX_INPUT:
        raise InvalidDocument("Максимальный размер DOCX для улучшения — 20 МБ.")
    name = PurePosixPath((document.file_name or "document.docx").replace("\\", "/")).stem[:100]
    name = "".join(ch for ch in name if ord(ch) >= 32) or "document"
    return PendingDocument(document.file_id, name, time.time())


def _command(text: str | None, context):
    match = COMMAND.fullmatch((text or "").strip())
    if match is None:
        return None
    target = match.group("target")
    username = getattr(context.bot, "username", None)
    if target and isinstance(username, str) and target.casefold() != username.casefold():
        raise InvalidDocument("Команда адресована другому боту. Используйте /clean с именами авторов.")
    return match


async def _private(update: Update) -> bool:
    if getattr(update.effective_chat, "type", None) == "private":
        return True
    await reply(update.effective_message, "Для улучшения DOCX отправьте документ в личный чат с ботом.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _private(update):
        return
    clear_pending(context)
    context.user_data.pop("semester", None)
    context.user_data["awaiting"] = AWAIT_FILE
    await reply(update.effective_message, INSTRUCTIONS)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _private(update):
        return
    cancelled = clear_pending(context)
    await reply(update.effective_message,
                "Улучшение DOCX отменено. Можно прислать запись или текст лекции."
                if cancelled else "Нет документа, ожидающего улучшения. Начать: /improve.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _private(update):
        return
    message = update.effective_message
    try:
        pending = _pending(message.document)
        command = _command(message.caption, context)
    except InvalidDocument as error:
        await reply(message, html(str(error)))
        return
    context.user_data.pop("semester", None)
    context.user_data[PENDING] = pending
    context.user_data["awaiting"] = AWAIT_AUTHORS
    if command is not None and command.group("authors"):
        try:
            request = parse_request(command.group("authors"))
        except InvalidDocument as error:
            await reply(message, html(str(error)))
            return
        await _process(update, context, pending, request.authors,
                       editing_minutes=request.editing_minutes, application_name=request.application_name)
    else:
        await reply(message, AUTHOR_PROMPT)


async def handle_authors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _private(update):
        return
    message = update.effective_message
    if context.user_data.get("awaiting") == AWAIT_FILE:
        await reply(message, "Сначала прикрепите документ .docx как файл. Отмена: /cancel.")
        return
    pending = context.user_data.get(PENDING)
    if not isinstance(pending, PendingDocument) or time.time() - pending.created > PENDING_TTL:
        clear_pending(context)
        await reply(message, "Время ожидания документа истекло. Пришлите DOCX снова или отправьте /improve.")
        return
    try:
        request = parse_request(message.text)
    except InvalidDocument as error:
        await reply(message, html(str(error)))
        return
    await _process(update, context, pending, request.authors,
                   editing_minutes=request.editing_minutes, application_name=request.application_name)


async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _private(update):
        return
    message = update.effective_message
    reply_to = getattr(message, "reply_to_message", None)
    document = getattr(reply_to, "document", None)
    try:
        command = _command(message.text, context)
        if document is not None:
            pending = _pending(document)
            context.user_data[PENDING] = pending
            context.user_data["awaiting"] = AWAIT_AUTHORS
        else:
            pending = context.user_data.get(PENDING)
        if not isinstance(pending, PendingDocument):
            await start(update, context)
            return
        context.user_data.pop("semester", None)
        if time.time() - pending.created > PENDING_TTL:
            clear_pending(context)
            await reply(message, "Время ожидания документа истекло. Пришлите DOCX снова.")
            return
        if command is None or not command.group("authors"):
            context.user_data["awaiting"] = AWAIT_AUTHORS
            await reply(message, AUTHOR_PROMPT)
            return
        request = parse_request(command.group("authors"))
    except InvalidDocument as error:
        await reply(message, html(str(error)))
        return
    await _process(update, context, pending, request.authors,
                   editing_minutes=request.editing_minutes, application_name=request.application_name)


def _read_local(path: Path) -> bytes:
    with path.open("rb") as file:
        return file.read(MAX_INPUT + 1)


def _report(counts) -> str:
    duration_label = "указанное время" if counts.get("minutes_manual") else "оценка проверки текста"
    created_label = "Дата создания сохранена" if counts.get("created_preserved") else "Дата создания установлена при обработке"
    text = (
        f"Файл приведён в порядок. Символов: {counts['symbols']}; {duration_label}: {counts['minutes']} мин. "
        f"{created_label}; дата изменения — момент обработки. Свойства не подтверждают историю работы. "
        f"Удалено невидимых символов: {counts['invisible']}; служебных маркеров: {counts['citations']}; "
        f"изменено ссылок: {counts['links']}. "
        f"Комментарии: {counts.get('comments', 0)}; принято исправлений: {counts.get('revisions', 0)}; "
        f"удалено скрытых фрагментов: {counts.get('hidden_runs', 0)}. "
    )
    if counts.get("fields"):
        text += f"Коды полей Word сохранены без обработки: {counts['fields']}. "
    return text + "Проверьте итог в Word. Метаданные внутри изображений не изменяются."


async def _process(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   pending: PendingDocument, authors: list[str], *, editing_minutes: int | None = None,
                   application_name: str = "DOCX Improver") -> None:
    message = update.effective_message
    user_id = update.effective_user.id
    s = services(context)
    ahead = s.queued.get(user_id, 0)
    if ahead >= MAX_QUEUED_PER_USER:
        await reply(message, "У вас уже пять заданий в очереди. Дождитесь их и повторите имена авторов.")
        return
    # Claim the current request before the first await. Concurrent updates cannot
    # process it twice or let an older job erase a newly received document.
    if context.user_data.get(PENDING) is not pending:
        return
    context.user_data.pop(PENDING)
    if context.user_data.get("awaiting") == AWAIT_AUTHORS:
        context.user_data.pop("awaiting", None)
    s.queued[user_id] = ahead + 1
    s.publish_active()
    try:
        await reply(message, "Документ в очереди на улучшение." if ahead else "Привожу DOCX в порядок…")
        lock = s.locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            async with s.slots:
                remote = await context.bot.get_file(pending.file_id, read_timeout=FILE_TIMEOUT)
                file_path = getattr(remote, "file_path", None)
                local = Path(file_path) if s.config.local_mode and file_path else None
                if local is not None and local.is_absolute() and local.is_file():
                    body = await asyncio.to_thread(_read_local, local)
                else:
                    body = bytes(await remote.download_as_bytearray(read_timeout=FILE_TIMEOUT))
                if len(body) > MAX_INPUT:
                    raise InvalidDocument("Максимальный размер DOCX для улучшения — 20 МБ.")
                # A thread must finish before active.jobs becomes zero, even if
                # the update task is cancelled during application shutdown.
                worker = asyncio.create_task(asyncio.to_thread(clean_docx, body, True, authors,
                                                               editing_minutes=editing_minutes,
                                                               application_name=application_name))
                try:
                    result, counts = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    await worker
                    raise
                await message.reply_document(document=io.BytesIO(result), filename=f"{pending.name}_improved.docx",
                                             caption=_report(counts))
    except InvalidDocument as error:
        await reply(message, html(str(error)))
    except Exception:
        # Transport exceptions may include token-bearing URLs; keep them out of logs.
        log.warning("DOCX improvement failed")
        await reply(message, "Не получилось обработать документ. Пришлите DOCX повторно или попробуйте позже.")
    finally:
        s.queued[user_id] -= 1
        if s.queued[user_id] <= 0:
            s.queued.pop(user_id, None)
        s.publish_active()
