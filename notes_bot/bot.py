from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import time
from pathlib import Path
from contextlib import suppress
from dataclasses import dataclass, replace

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import admin
from .audio import is_media, sniff
from .config import Config
from .agreement import ACCEPT_PREFIX, AGREEMENT, AGREEMENT_VERSION
from .gateway import GatewayClient, RequestLimiter
from .gemini import GeminiClient, GeminiError
from .keys import KEY_PATTERN, KeyVault, is_gateway_key
from .pdf_text import NoTextError, pdf_text
from .menus import (
    AI_STUDIO,
    MENU_ADMIN,
    MENU_HELP,
    MENU_KEYS,
    MENU_SETTINGS,
    apply_choice,
    color_prompt,
    confirm_clear,
    help_text,
    key_prompt,
    keys_view,
    main_keyboard,
    menu_label,
    settings_view,
    welcome,
)
from .gemini import GeminiError
from .notes import document_text
from .pipeline import NotesPipeline, Result, Source
from .render_pdf import render_pdf
from .render_tg import fit_one_message, plain_text
from .services import ADDED_OUTCOMES, SHARED, Services, add_keys, check_keys, edit, key_report, reply, services
from .settings import SettingsStore, UserSettings
from .storage import BotState, Options
from .topics import rank
from .themes import CUSTOM, get_theme, parse_hex
from .ui import Ui, html

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram's cloud Bot API refuses to give the bot bigger files
LOCAL_MAX_FILE_BYTES = 2000 * 1024 * 1024  # a local Bot API server allows up to 2 GB
# Getting a big file from a local server means it is downloaded from Telegram first; that can take minutes.
FILE_TIMEOUT = 900
TEXT_EXTENSIONS = (".txt", ".md")
STAGES = {
    "queued": ("clock", "В очереди"),
    "transcribing": ("transcribing", "Расшифровываю запись…"),
    "writing": ("writing", "Пишу конспект…"),
    "outlining": ("writing", "Делю материал на темы…"),
    "rendering": ("rendering", "Оформляю…"),
}
AWAIT_KEY, AWAIT_COLOR = "key", "color"
# How many notes one user may have waiting or running at once.
MAX_QUEUED_PER_USER = 5
# Files sent together (an album, or several in a row) are collected for this long before the bot asks what to do.
BATCH_WINDOW = 2.5
MAX_BATCH = 10
PENDING_TTL = 3600
USER_COMMANDS = [
    BotCommand("start", "Главное меню"),
    BotCommand("settings", "Настройки конспектов"),
    BotCommand("keys", "Ключи Gemini"),
    BotCommand("help", "Как пользоваться"),
]
ADMIN_COMMANDS = [*USER_COMMANDS, BotCommand("admin", "Админ-панель")]


# --- access -----------------------------------------------------------------------------


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler: records the user and stops strangers, banned users and everyone during maintenance."""
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    s = services(context)
    if s.state.touch(user.id, user.username, user.full_name) and s.options().notify_new_users:
        context.application.create_task(admin.notify_new_user(context, s, user))
    problem = s.access_problem(user.id)
    if problem is None:
        return
    if update.callback_query is not None:
        await update.callback_query.answer("Доступ закрыт", show_alert=True)
    elif update.effective_message is not None:
        ui = s.ui()
        if problem == "maintenance":
            text = f"{ui.e('sleep')} Бот на обслуживании. Попробуйте чуть позже."
        elif problem == "banned":
            text = f"{ui.e('ban')} Доступ к боту закрыт."
        else:
            text = (f"{ui.e('locked')} Доступ закрыт. Ваш ID: <code>{user.id}</code>. "
                    "Попросите владельца бота добавить вас.")
        await reply(update.effective_message, text)
    raise ApplicationHandlerStop


# --- commands and main menu -------------------------------------------------------------


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    user_id = update.effective_user.id
    context.user_data.pop("awaiting", None)
    ui = s.ui()
    await reply(
        update.effective_message,
        welcome(ui, s.keys.count(user_id) > 0, s.options().shared_keys, s.config.local_mode),
        main_keyboard(ui, s.is_admin(user_id)),
    )
    if not await _agreed(update, context):
        return
    if s.is_admin(user_id):
        with suppress(TelegramError):
            await context.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(update.effective_chat.id))


async def _agreed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the user accepted the current agreement. Otherwise shows it and returns False."""
    s = services(context)
    user_id = update.effective_user.id
    if s.state.agreed_version(user_id) == AGREEMENT_VERSION:
        return True
    ui = s.ui()
    keyboard = InlineKeyboardMarkup([[ui.button("ok", "Принимаю", ACCEPT_PREFIX + AGREEMENT_VERSION, style="success")]])
    await reply(update.effective_message, f"{ui.e('info')} {AGREEMENT}", keyboard)
    return False


async def on_agreement_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = services(context)
    ui = s.ui()
    version = query.data.removeprefix(ACCEPT_PREFIX)
    if version != AGREEMENT_VERSION:
        await query.answer("Соглашение изменилось, прочитайте новое", show_alert=True)
        await edit(query, f"{ui.e('info')} {AGREEMENT}", InlineKeyboardMarkup([[
            ui.button("ok", "Принимаю", ACCEPT_PREFIX + AGREEMENT_VERSION, style="success")]]))
        return
    s.state.accept_agreement(query.from_user.id, version)
    await query.answer()
    await edit(query, f"{ui.e('ok')} Спасибо, соглашение принято. Пришлите запись или текст лекции.")


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    ui = s.ui()
    await reply(update.effective_message, help_text(ui, s.config.local_mode), main_keyboard(ui, s.is_admin(update.effective_user.id)))


async def on_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    context.user_data.pop("awaiting", None)
    await reply(update.effective_message, *settings_view(s.ui(), "main", s.store.get(update.effective_user.id)))


async def on_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    context.user_data.pop("awaiting", None)
    await reply(update.effective_message, *_keys_view(s, update.effective_user.id))


async def on_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    context.user_data["awaiting"] = AWAIT_KEY
    await reply(update.effective_message, *key_prompt(s.ui(), "ky:show"))


async def on_forgetkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    s.keys.clear(update.effective_user.id)
    await reply(update.effective_message, f"{s.ui().e('trash')} Все ваши ключи удалены. Добавить новый: /keys.")


# --- settings ---------------------------------------------------------------------------


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    s = services(context)
    ui = s.ui()
    user_id = query.from_user.id
    settings = s.store.get(user_id)
    _, action, *args = query.data.split(":")
    context.user_data.pop("awaiting", None)

    if action == "color":
        context.user_data["awaiting"] = AWAIT_COLOR
        await edit(query, *color_prompt(ui))
        return
    if action == "close":
        await edit(query, f"{ui.e('ok')} Настройки сохранены. Пришлите запись или текст, и я сделаю конспект.")
        return

    view = "main"
    if action == "menu":
        view = args[0]
    elif action == "set":
        field, value = args
        settings = apply_choice(settings, field, value)
        view = field
    elif action == "ext":
        settings = settings.toggle_extra(args[0])
        view = "extras"
    elif action == "raw":
        settings = replace(settings, ask_raw=not settings.ask_raw)
    elif action == "reset":
        settings = UserSettings()
    s.store.save(user_id, settings)
    await edit(query, *settings_view(ui, view, settings))


async def _save_color(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    s = services(context)
    ui = s.ui()
    color = parse_hex(raw)
    if color is None:
        context.user_data["awaiting"] = AWAIT_COLOR
        await reply(update.effective_message,
                    f"{ui.e('warning')} Не понял цвет или он слишком светлый. Пример: <code>#3B82F6</code>")
        return
    user_id = update.effective_user.id
    settings = replace(s.store.get(user_id), theme=CUSTOM, custom_color=color)
    s.store.save(user_id, settings)
    text, keyboard = settings_view(ui, "theme", settings)
    await reply(update.effective_message, f"{ui.e('ok')} Цвет {color} сохранён.\n\n{text}", keyboard)


# --- keys -------------------------------------------------------------------------------


def _keys_view(s: Services, user_id: int, statuses: dict[int, str] | None = None):
    return keys_view(s.ui(), s.keys.list(user_id), s.ring, s.keys.limit(user_id), statuses=statuses)


async def on_keys_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = services(context)
    ui = s.ui()
    user_id = query.from_user.id
    _, command, *args = query.data.split(":")
    context.user_data.pop("awaiting", None)

    if command == "add":
        await query.answer()
        context.user_data["awaiting"] = AWAIT_KEY
        await edit(query, *key_prompt(ui, "ky:show"))
        return
    if command == "del":
        s.keys.remove(user_id, int(args[0]))
        await query.answer("Ключ удалён")
    elif command == "clear":
        if args != ["yes"]:
            await query.answer()
            await edit(query, *confirm_clear(ui, "ky"))
            return
        s.keys.clear(user_id)
        await query.answer("Все ключи удалены")
    elif command == "check":
        await query.answer("Проверяю ключи…")
        await edit(query, *_keys_view(s, user_id, await check_keys(s, user_id)))
        return
    else:
        await query.answer()
    await edit(query, *_keys_view(s, user_id))


async def _save_keys(update: Update, context: ContextTypes.DEFAULT_TYPE, found: list[str]) -> None:
    s = services(context)
    ui = s.ui()
    message = update.effective_message
    user_id = update.effective_user.id
    status = await reply(message, f"{ui.e('clock')} Проверяю {'ключи' if len(found) > 1 else 'ключ'}…")
    report = await add_keys(s, user_id, found)
    added = any(outcome in ADDED_OUTCOMES for _, outcome in report)
    if added:
        context.user_data.pop("awaiting", None)
    text, keyboard = _keys_view(s, user_id)
    tail = ("\n\nТеперь пришлите запись или текст лекции. Удалите сообщение с ключом из чата: бот не может сделать это сам."
            if added else "")
    await edit(status, f"{key_report(ui, report)}{tail}\n\n{text}", keyboard)


# --- text and files ---------------------------------------------------------------------


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    text = update.effective_message.text
    awaiting = context.user_data.get("awaiting")

    label = menu_label(text)
    if label is not None:
        context.user_data.pop("awaiting", None)
        if label == MENU_SETTINGS:
            await on_settings(update, context)
        elif label == MENU_KEYS:
            await on_keys(update, context)
        elif label == MENU_HELP:
            await on_help(update, context)
        elif label == MENU_ADMIN:
            await admin.on_admin(update, context)
        return

    if awaiting and awaiting.startswith("admin:"):
        await admin.handle_awaited(update, context, awaiting)
        return
    found = KEY_PATTERN.findall(text)
    if awaiting == AWAIT_KEY:
        if found:
            await _save_keys(update, context, found)
        else:
            await reply(update.effective_message, f"{s.ui().e('warning')} Не нашёл ключа. Ключи Gemini начинаются "
                                                  "с <code>AIza</code> или <code>AQ.</code>")
        return
    if awaiting == AWAIT_COLOR:
        context.user_data.pop("awaiting", None)
        await _save_color(update, context, text)
        return
    # A message that is nothing but keys adds them without /keys first.
    if found and not KEY_PATTERN.sub("", text).strip():
        await _save_keys(update, context, found)
        return
    if not await _agreed(update, context):
        return
    await _run_job(update, context, [Source(text=text)])


def _attachment(message: Message) -> tuple[object, str | None, str | None] | None:
    """The file, its name and its MIME type."""
    if message.voice:
        return message.voice, "voice.ogg", message.voice.mime_type
    if message.audio:
        return message.audio, message.audio.file_name, message.audio.mime_type
    if message.video_note:
        return message.video_note, "video_note.mp4", "video/mp4"
    if message.video:
        return message.video, message.video.file_name, message.video.mime_type
    if message.document:
        return message.document, message.document.file_name, message.document.mime_type
    return None


def _is_pdf(name: str | None, mime: str | None) -> bool:
    return (name or "").lower().endswith(".pdf") or (mime or "").lower() == "application/pdf"


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _describe(message: Message, name: str | None) -> str:
    """A short label for the file list in the question."""
    def minutes(seconds: int | None) -> str:
        return f", {seconds // 60}:{seconds % 60:02d}" if seconds else ""

    if message.voice:
        return "Голосовое" + minutes(message.voice.duration)
    if message.video_note:
        return "Кружок" + minutes(message.video_note.duration)
    if message.audio:
        return (name or message.audio.title or "Аудио") + minutes(message.audio.duration)
    if message.video:
        return (name or "Видео") + minutes(message.video.duration)
    return name or "Файл"


@dataclass(frozen=True)
class Incoming:
    message_id: int
    source: Source
    label: str


@dataclass(frozen=True)
class Pending:
    token: str
    items: list[Incoming]
    created: float


def _local_path(file) -> Path | None:
    """A local Bot API server hands out paths on the server's own disk."""
    path = Path(file.file_path) if file.file_path else None
    return path if path is not None and path.is_absolute() and path.is_file() else None


def _head(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(64)


SEMESTER = "/semester"


def _asks_topics(text: str | None) -> bool:
    """/semester at the start of a text or a file's caption: one note per topic, however long the material is."""
    return (text or "").strip().lower().startswith(SEMESTER)


def _all_subjects(s: Services) -> list[str]:
    return [subject for _, _, topics in s.state.groups_with_topics() for subject, _ in topics]


SUBJECT_MAX = 40


def _split_semester(body: str, subjects: list[str]) -> tuple[str | None, str, str | None]:
    """What follows /semester: a subject (on its own first line, or alone), the text, and an unknown subject if the
    first line looks like one but matches nothing. A subject line is short and does not end like a sentence."""
    stripped = body.strip()
    head, newline, rest = stripped.partition("\n")
    head = head.strip()
    if not head or len(head) > SUBJECT_MAX or head[-1] in ".?!":
        return None, stripped, None
    for subject in subjects:
        if head.lower() == subject.lower():
            return subject, rest.strip() if newline else "", None
    return None, stripped, head


async def _direct_target(bot, s: Services, user_id: int, subject: str) -> tuple[int, int, str] | None:
    """Where a subject goes for this user: the group topic, if the user is in that group."""
    for chat_id, title, topics in s.state.groups_with_topics():
        for name, thread_id in topics:
            if name.lower() == subject.lower() and await _is_member(bot, s, chat_id, user_id):
                return chat_id, thread_id, title
    return None


async def on_semester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/semester [предмет] и текст: конспект на каждую тему. С предметом все они уходят в его раздел."""
    message = update.effective_message
    s = services(context)
    body = re.sub(r"^/semester(@\S+)?", "", message.text or "", count=1)
    subject, text, unknown = _split_semester(body, _all_subjects(s))
    if not text and unknown is None:
        await reply(message, f"Напишите <code>{SEMESTER}</code>, с новой строки название предмета (если нужно), "
                             "а дальше текст лекций. Можно и файлом с такой подписью.")
        return
    if not await _agreed(update, context):
        return
    await _run_job(update, context, [Source(text=text, split=True, subject=subject or unknown)])


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    ui = s.ui()
    message = update.effective_message
    awaiting = context.user_data.get("awaiting")
    if awaiting == admin.AWAIT_BROADCAST:
        await admin.handle_awaited(update, context, awaiting)
        return
    found = _attachment(message)
    if found is None or message.animation:
        return
    if not await _agreed(update, context):
        return
    attachment, name, mime = found
    limit = LOCAL_MAX_FILE_BYTES if s.config.local_mode else MAX_FILE_BYTES
    if (attachment.file_size or 0) > limit:
        hint = "Разбейте запись на части." if s.config.local_mode else "Сожмите запись или разбейте её на части."
        await reply(message, f"{ui.e('warning')} Файл больше {limit // (1024 * 1024)} МБ, "
                             f"Telegram не даёт боту его скачать. {hint}")
        return

    is_pdf = _is_pdf(name, mime)
    split = _asks_topics(message.caption)
    caption_subject = None
    if split:
        subject, _, unknown = _split_semester((message.caption or "")[len(SEMESTER):], _all_subjects(services(context)))
        caption_subject = subject or unknown
    is_text = (name or "").lower().endswith(TEXT_EXTENSIONS) or (mime or "").startswith("text/plain")
    # While a download runs, nobody flushes the batch: the file may belong to it.
    data = context.user_data
    data["receiving"] = data.get("receiving", 0) + 1
    path: Path | None = None
    body: bytes | None = None
    try:
        file = await attachment.get_file(read_timeout=FILE_TIMEOUT)
        path = _local_path(file)
        if path is None or is_text or is_pdf:
            body = bytes(await file.download_as_bytearray())
            path = None
    except TelegramError:
        log.exception("Could not download a file")
    except OSError:
        log.exception("Could not read a file from the local Bot API server")
    finally:
        data["receiving"] -= 1

    if body is None and path is None:
        await reply(message, f"{ui.e('warning')} Не получилось скачать файл. Пришлите его ещё раз.")
    elif is_pdf and body is not None:
        try:
            text = await asyncio.to_thread(pdf_text, body)
        except NoTextError:
            await reply(message, f"{ui.e('warning')} В PDF нет текста, похоже, это скан. Пришлите текст или запись.")
        else:
            data.setdefault("batch", []).append(Incoming(message.message_id, Source(text=text, split=split, subject=caption_subject), name or "PDF"))
    elif is_text:
        data.setdefault("batch", []).append(Incoming(message.message_id, Source(text=_decode(body), split=split, subject=caption_subject), name or "Текст"))
    elif not message.document or is_media(name, mime) or sniff(body if body is not None else _head(path)):
        # A document without a telling name or type may still be a recording: the header decides.
        source = Source(audio=body, name=name, mime=mime, path=path, split=split, subject=caption_subject)
        data.setdefault("batch", []).append(Incoming(message.message_id, source, _describe(message, name)))
    else:
        await reply(message, f"{ui.e('warning')} Не понимаю этот файл. Пришлите аудио, видео или текст в формате .txt.")
    await _flush_later(update, context)


async def _flush_later(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wait a moment for more files. The handler of the last file decides: one file runs at once, several get a question."""
    data = context.user_data
    seq = data["batch_seq"] = data.get("batch_seq", 0) + 1
    await asyncio.sleep(BATCH_WINDOW)
    if data.get("batch_seq") != seq or data.get("receiving", 0) > 0 or not data.get("batch"):
        return
    items = sorted(data.pop("batch"), key=lambda item: item.message_id)
    pending: Pending | None = data.pop("pending", None)
    if pending is not None and time.time() - pending.created < PENDING_TTL:
        items = pending.items + items  # files sent while the question was open join it
    asks_raw = services(context).store.get(update.effective_user.id).ask_raw and any(item.source.is_audio for item in items)
    if len(items) == 1 and not asks_raw:
        await _run_job(update, context, [items[0].source])
        return
    await _ask_about_batch(update.effective_message, context, items)


async def _ask_about_batch(message: Message, context: ContextTypes.DEFAULT_TYPE, items: list[Incoming]) -> None:
    ui = services(context).ui()
    dropped = len(items) - MAX_BATCH
    items = items[:MAX_BATCH]
    token = secrets.token_hex(4)
    context.user_data["pending"] = Pending(token, items, time.time())
    lines = [f"{ui.e('clip')} <b>Пришло файлов: {len(items)}</b>", ""]
    lines += [f"{number}. {html(item.label)}" for number, item in enumerate(items, start=1)]
    if dropped > 0:
        lines += ["", f"Беру первые {MAX_BATCH}, остальные {dropped} пришлите отдельно."]
    rows = []
    if len(items) > 1:
        lines += ["", "Это одна лекция по частям или разные? Части склеиваются в порядке отправки."]
        rows.append([ui.button("stack", "Одна лекция", f"bt:merge:{token}", style="success"),
                     ui.button("list", "Отдельные конспекты", f"bt:split:{token}", style="primary")])
    else:
        lines += ["", "Сделать конспект?"]
        rows.append([ui.button("stack", "Сделать конспект", f"bt:merge:{token}", style="success")])
    if any(item.source.is_audio for item in items):
        lines += ["Или пришлю только текст расшифровки записей, без конспекта."]
        rows.append([ui.button("transcript", "Только расшифровка", f"bt:raw:{token}")])
    rows.append([ui.button("no", "Отмена", f"bt:cancel:{token}")])
    keyboard = InlineKeyboardMarkup(rows)
    await reply(message, "\n".join(lines), keyboard)


async def on_batch_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    ui = services(context).ui()
    _, action, token = query.data.split(":")
    pending: Pending | None = context.user_data.get("pending")
    if pending is None or pending.token != token:
        await query.answer("Эти файлы уже не ждут ответа", show_alert=True)
        await edit(query, f"{ui.e('info')} Этот вопрос устарел. Пришлите файлы ещё раз.")
        return
    context.user_data.pop("pending")
    await query.answer()
    count = len(pending.items)
    if action == "merge":
        await edit(query, f"{ui.e('stack')} Делаю один конспект из {count} частей.")
        await _run_job(update, context, [item.source for item in pending.items])
    elif action == "raw":
        await edit(query, f"{ui.e('transcript')} Расшифровываю, пришлю только текст.")
        await _run_job(update, context, [item.source for item in pending.items], raw=True)
    elif action == "split":
        await edit(query, f"{ui.e('list')} Делаю {count} отдельных конспектов, по очереди.")
        for item in pending.items:
            await _run_job(update, context, [item.source])
    else:
        await edit(query, f"{ui.e('no')} Отменено, файлы забыты.")


# --- jobs -------------------------------------------------------------------------------


def no_key_text(ui: Ui) -> str:
    return (f"{ui.e('key')} Чтобы делать конспекты, нужен ключ Gemini. Он бесплатный: создайте его в "
            f'<a href="{AI_STUDIO}">Google AI Studio</a> и пришлите сюда одним сообщением.')


def _stage(ui: Ui, stage: str, detail: str = "") -> str:
    icon, text = STAGES[stage]
    if detail:
        text = f"{text.removesuffix('…')} {detail}…"
    return f"{ui.e(icon)} {text}"


def _notes(count: int) -> str:
    """'1 конспект', '3 конспекта', '5 конспектов'."""
    if count % 10 == 1 and count % 100 != 11:
        word = "конспект"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "конспекта"
    else:
        word = "конспектов"
    return f"{count} {word}"


def _queue_text(ui: Ui, ahead: int, from_audio: bool) -> str:
    if ahead == 0:
        return _stage(ui, "transcribing" if from_audio else "writing")
    return f"{ui.e('clock')} В очереди: перед вами {_notes(ahead)}. Начну, как освободится."


async def _run_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    sources: list[Source],
    raw: bool = False,
) -> None:
    """Make one note from the sources. A user's notes run one after another, in the order they were sent."""
    message = update.effective_message
    user_id = update.effective_user.id
    s = services(context)
    ui = s.ui()
    options = s.options()

    model, source = s.model_for(user_id)
    if model is None:
        await reply(message, no_key_text(ui))
        return
    for item in sources:
        if item.subject and await _direct_target(context.bot, s, user_id, item.subject) is None:
            await reply(message, f"{ui.e('warning')} Раздела «{html(item.subject)}» нет в группах, где вы состоите. "
                                 "Проверьте название или /topics.")
            return
    if options.daily_limit and not s.is_admin(user_id) and s.state.notes_last_day(user_id) >= options.daily_limit:
        await reply(message, f"{ui.e('speed')} Лимит: {options.daily_limit} конспектов за сутки. Попробуйте позже.")
        return

    ahead = s.queued.get(user_id, 0)
    if ahead >= MAX_QUEUED_PER_USER:
        await reply(message, f"{ui.e('clock')} У вас уже в очереди {_notes(ahead)}. Дождитесь, пожалуйста, их.")
        return
    s.queued[user_id] = ahead + 1
    s.publish_active()
    try:
        status = await reply(message, _queue_text(ui, ahead, any(item.is_audio for item in sources)))
        lock = s.locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            await _make_note(message, status, s, user_id, model, source, sources, ui, options, raw, context.bot)
    finally:
        s.queued[user_id] -= 1
        if s.queued[user_id] <= 0:
            s.queued.pop(user_id, None)
        s.publish_active()


async def _make_note(message, status, s, user_id, model, source, sources, ui, options, raw=False, bot=None) -> None:
    settings = s.store.get(user_id)
    from_audio = any(item.is_audio for item in sources)

    async def progress(stage: str, detail: str = "") -> None:
        await _status(status, _stage(ui, stage, detail))

    async def keep_transcript(text: str) -> None:
        # Sent as soon as it exists, so a failure while writing the note still leaves the user with the text.
        with suppress(TelegramError):
            await message.reply_document(document=io.BytesIO(text.encode("utf-8")), filename=_transcript_name(sources))

    await _status(status, _stage(ui, "transcribing" if from_audio else "writing"))
    kind = "audio" if from_audio else "text"
    started = time.monotonic()
    async with s.slots:
        try:
            if raw:
                await NotesPipeline(model).read(
                    sources=sources, progress=progress, on_transcript=keep_transcript, language=settings.language,
                )
                s.state.log_job(user_id, kind, True, time.monotonic() - started)
                await _status(status, f"{ui.e('done')} Расшифровка готова")
                return
            results = await NotesPipeline(model).run_parts(
                settings, progress=progress, sources=sources, premium=options.premium_emoji,
                on_transcript=keep_transcript,
            )
        except GeminiError as error:
            s.state.log_job(user_id, kind, False, time.monotonic() - started, str(error))
            await _status(status, f"{ui.e('warning')} {html(str(error))}")
            return
        except Exception as error:
            log.exception("Failed to build notes for user %s", user_id)
            s.state.log_job(user_id, kind, False, time.monotonic() - started, repr(error))
            await _status(status, f"{ui.e('warning')} Не получилось сделать конспект. Попробуйте ещё раз чуть позже.")
            return

    s.state.log_job(user_id, kind, True, time.monotonic() - started)
    notes = []
    if source == SHARED:
        notes.append("на общем ключе")
    if len(results) > 1:
        notes.append(f"{len(results)} конспектов по темам")
    await _status(status, f"{ui.e('done')} Конспект готов" + (f" ({', '.join(notes)})" if notes else ""))
    subject = next((item.subject for item in sources if item.subject), None)
    target = await _direct_target(bot, s, user_id, subject) if subject else None
    for result in results:
        await _send(message, result)
        if subject is None:
            await _offer_topics(message, bot, s, user_id, result)
        elif target is not None:
            chat_id, thread_id, _ = target
            try:
                await _post_to_topic(bot, chat_id, thread_id, _group_result(s, user_id, result))
            except TelegramError:
                log.exception("Could not post a semester note to topic %s of chat %s", thread_id, chat_id)
                await message.reply_text(f"{ui.e('warning')} Не получилось отправить один из конспектов в раздел "
                                         f"«{html(subject)}». Проверьте, что бот в группе и раздел не удалён.")
    if subject is not None and target is not None:
        await _status(status, f"{ui.e('done')} Конспекты отправлены в раздел «{html(subject)}»: {len(results)}")


async def _status(status: Message, text: str) -> None:
    with suppress(TelegramError):  # a lost status line must not stop the job
        await edit(status, text)


def _transcript_name(sources) -> str:
    names = [Path(item.name).stem for item in sources if item.is_audio and item.name]
    return f"Расшифровка — {_file_name(names[0])}.txt" if len(names) == 1 else "Расшифровка.txt"


def _file_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()
    return (cleaned or "Конспект")[:80]


POST_TTL = 3600
MEMBERSHIP_TTL = 600
MEMBER_STATUSES = ("creator", "administrator", "member", "restricted")


@dataclass(frozen=True)
class Post:
    created: float
    user_id: int
    result: Result  # already in the group's format: PDF and one short message
    targets: list[tuple[str, int, int]]  # (subject, chat id, topic id) in the order the buttons show them


async def _is_member(bot, s: Services, chat_id: int, user_id: int) -> bool:
    """Whether the user is in the group right now. Telegram answers; the answer is kept for a few minutes."""
    now = time.time()
    cached = s.membership.get((chat_id, user_id))
    if cached is not None and now - cached[0] < MEMBERSHIP_TTL:
        return cached[1]
    try:
        member = (await bot.get_chat_member(chat_id, user_id)).status in MEMBER_STATUSES
    except TelegramError:
        log.warning("Could not check membership of %s in %s", user_id, chat_id)
        return False  # not cached: the next note asks again
    s.membership[(chat_id, user_id)] = (now, member)
    return member


def _group_result(s: Services, user_id: int, result: Result) -> Result:
    """What the group gets: the PDF and one short message, whatever format the user chose for the chat."""
    settings = s.store.get(user_id)
    options = s.options()
    pdf = result.pdf
    if pdf is None:
        theme = get_theme(settings.theme, settings.custom_color)
        pdf = render_pdf(result.document, theme)
    if settings.output in ("both_one", "tg_one") and result.messages:
        short = result.messages
    else:
        short = fit_one_message(result.document, premium=options.premium_emoji)
    return Result(result.document, result.transcript, pdf=pdf, messages=short)


async def _offer_topics(message: Message, bot, s: Services, user_id: int,
                        result: Result) -> None:
    """After the note reached the user's chat, offer the groups the user is in. Nothing is sent without a tap."""
    groups = [group for group in s.state.groups_with_topics() if await _is_member(bot, s, group[0], user_id)]
    if not groups:
        return
    ui = s.ui()
    now = time.time()
    s.posts = {token: post for token, post in s.posts.items() if now - post.created < POST_TTL}
    entries = [(subject, chat_id, thread_id, title)
               for chat_id, title, topics in groups for subject, thread_id in topics]
    order, matched = rank([subject for subject, _, _, _ in entries], document_text(result.document))
    entries = [entries[index] for index in order]
    targets = [(subject, chat_id, thread_id) for subject, chat_id, thread_id, _ in entries]
    token = secrets.token_hex(4)
    s.posts[token] = Post(now, user_id, _group_result(s, user_id, result), targets)
    several = len(groups) > 1
    rows = []
    for index, (subject, _, _, title) in enumerate(entries):
        label = f"{subject} · {title}" if several else subject
        if index < matched:
            label = f"✅ {label}"
        rows.append([ui.button("list", label, f"tp:{token}:{index}", style="success" if index < matched else None)])
    rows.append([ui.button("no", "Не отправлять в группу", f"tp:{token}:x")])
    hint = ("Бот подобрал раздел по тексту, ✅ отмечен." if matched
            else "Бот не угадал предмет по тексту, выберите раздел сами.")
    await reply(message, f"{ui.e('clip')} <b>Отправить этот конспект в группу?</b>\n{hint}",
                InlineKeyboardMarkup(rows))


async def on_topic_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = services(context)
    ui = s.ui()
    _, token, choice = query.data.split(":")
    post: Post | None = s.posts.get(token)
    if post is None or post.user_id != query.from_user.id:
        await query.answer("Этот вопрос устарел", show_alert=True)
        await edit(query, f"{ui.e('info')} Этот вопрос устарел. Конспект можно отправить в группу, сделав его заново.")
        return
    s.posts.pop(token, None)
    await query.answer()
    if choice == "x":
        await edit(query, f"{ui.e('no')} Не отправлено в группу.")
        return
    subject, chat_id, thread_id = post.targets[int(choice)]
    if not await _is_member(context.bot, s, chat_id, post.user_id):
        await edit(query, f"{ui.e('warning')} Вы больше не в этой группе, отправлять туда нельзя.")
        return
    try:
        await _post_to_topic(context.bot, chat_id, thread_id, post.result)
    except TelegramError:
        log.exception("Could not post a note to topic %s of chat %s", thread_id, chat_id)
        await edit(query, f"{ui.e('warning')} Не получилось отправить в раздел «{html(subject)}». "
                          "Проверьте, что бот в группе и раздел не удалён.")
        return
    await edit(query, f"{ui.e('done')} Отправлено в раздел «{html(subject)}».")


async def _post_to_topic(bot, chat_id: int, thread_id: int, result: Result) -> None:
    name = _file_name(result.document.title)
    for text in result.messages or []:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2, message_thread_id=thread_id)
        except BadRequest:
            await bot.send_message(chat_id, plain_text(text), message_thread_id=thread_id)
    if result.pdf is not None:
        await bot.send_document(chat_id, document=io.BytesIO(result.pdf), filename=f"{name}.pdf",
                                message_thread_id=thread_id)


def _group_owner_check(s: Services, message: Message, user_id: int) -> bool:
    """The group's topics are set up by the one who first set them up. Others in the group only use them."""
    owner = s.state.group_owner(message.chat.id)
    return owner is None or owner == user_id


async def on_bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """In a group topic: /bind Физика makes that topic the place for physics notes of the group."""
    message = update.effective_message
    s = services(context)
    user_id = update.effective_user.id
    subject = (message.text or "").partition(" ")[2].strip()
    if message.chat.type not in ("group", "supergroup"):
        await reply(message, "Команда /bind нужна в группе, внутри раздела, который вы хотите привязать.")
        return
    if not _group_owner_check(s, message, user_id):
        await reply(message, "Разделы этой группы настраивает тот, кто их первым привязал.")
        return
    if not subject:
        await reply(message, "Напишите название предмета: <code>/bind Физика</code>")
        return
    if message.message_thread_id is None:
        await reply(message, "Напишите /bind внутри самого раздела, а не в общем чате группы.")
        return
    s.state.claim_group(message.chat.id, getattr(message.chat, "title", None) or "", user_id)
    s.state.bind_topic(message.chat.id, subject[:60], message.message_thread_id)
    await reply(message, f"{s.ui().e('ok')} Раздел привязан к предмету «{html(subject[:60])}». "
                         "Конспекты по нему смогут отправлять все участники группы.")


async def on_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    s = services(context)
    subject = (message.text or "").partition(" ")[2].strip()
    if message.chat.type not in ("group", "supergroup"):
        await reply(message, "Команду /unbind пишут в группе, у раздела которой надо снять предмет.")
        return
    if not _group_owner_check(s, message, update.effective_user.id):
        await reply(message, "Разделы этой группы настраивает тот, кто их первым привязал.")
        return
    if s.state.unbind_topic(message.chat.id, subject):
        await reply(message, f"{s.ui().e('ok')} Предмет «{html(subject)}» отвязан от раздела.")
    else:
        await reply(message, f"{s.ui().e('info')} Такого предмета в этой группе нет.")


async def on_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    s = services(context)
    user_id = update.effective_user.id
    lines = []
    for chat_id, title, topics in s.state.groups_with_topics():
        if await _is_member(context.bot, s, chat_id, user_id):
            subjects = ", ".join(html(subject) for subject, _ in topics)
            lines.append(f"• <b>{html(title or 'группа')}</b>: {subjects}")
    if not lines:
        await reply(message, "Вы пока не состоите в группах с разделами. Когда вас добавят в такую группу, "
                             "конспекты будут предлагаться её разделам.")
        return
    await reply(message, "<b>Группы с разделами, где вы состоите:</b>\n" + "\n".join(lines))


async def _send(message: Message, result: Result) -> None:
    name = _file_name(result.document.title)
    # The readable messages first, so the note is there at once; the PDF follows when both are asked for.
    for text in result.messages or []:
        try:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest:
            log.warning("Telegram rejected a MarkdownV2 message; sending it as plain text")
            await message.reply_text(plain_text(text))
    if result.pdf is not None:
        await message.reply_document(document=io.BytesIO(result.pdf), filename=f"{name}.pdf")
# --- application ------------------------------------------------------------------------


async def _post_init(application: Application) -> None:
    s: Services = application.bot_data["services"]
    s.publish_active()  # a fresh start has no notes running
    try:
        await application.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    except TelegramError:
        log.warning("Could not set the bot command menu")
        return
    for admin_id in s.state.admins():
        with suppress(TelegramError):  # an admin who never opened the bot has no chat yet
            await application.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(admin_id))


def _client_factory(config: Config):
    """Keys of the resold gateway go to their own client; everything else is a Google AI Studio key."""
    limiters: dict[str, RequestLimiter] = {}  # one per gateway key, shared by every job that uses it

    def make_client(api_key: str, model: str):
        if is_gateway_key(api_key):
            return GatewayClient(api_key, config.gateway_model, limiter=limiters.setdefault(api_key, RequestLimiter()))
        return GeminiClient(api_key, model)

    return make_client


def build_services(config: Config) -> Services:
    return Services(
        config=config,
        store=SettingsStore(config.data_dir / "settings.sqlite3"),
        keys=KeyVault(config.data_dir),
        state=BotState(
            config.data_dir / "bot.sqlite3",
            Options(model=config.gemini_model),
            admin_ids=config.admin_ids,
            allowed_ids=config.allowed_user_ids,
        ),
        make_client=_client_factory(config),
    )


def build_application(config: Config) -> Application:
    builder = Application.builder().token(config.telegram_token).concurrent_updates(True).post_init(_post_init)
    if config.local_mode:
        builder = (
            builder.base_url(f"{config.telegram_api_url}/bot")
            .base_file_url(f"{config.telegram_api_url}/file/bot")
            .local_mode(True)
        )
    application = builder.build()
    application.bot_data["services"] = build_services(config)
    application.add_handler(TypeHandler(Update, gate), group=-1)
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("help", on_help))
    application.add_handler(CommandHandler("settings", on_settings))
    application.add_handler(CommandHandler("keys", on_keys))
    application.add_handler(CommandHandler("setkey", on_setkey))
    application.add_handler(CommandHandler("forgetkey", on_forgetkey))
    application.add_handler(CommandHandler("admin", admin.on_admin))
    application.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^st:"))
    application.add_handler(CallbackQueryHandler(on_keys_button, pattern=r"^ky:"))
    application.add_handler(CallbackQueryHandler(on_batch_button, pattern=r"^bt:"))
    application.add_handler(CallbackQueryHandler(on_agreement_button, pattern=r"^ag:"))
    application.add_handler(CallbackQueryHandler(on_topic_button, pattern=r"^tp:"))
    application.add_handler(CommandHandler("bind", on_bind))
    application.add_handler(CommandHandler("unbind", on_unbind))
    application.add_handler(CommandHandler("topics", on_topics))
    application.add_handler(CommandHandler("semester", on_semester, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(admin.on_admin_button, pattern=r"^ad:"))
    application.add_handler(
        MessageHandler(
            (filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL
             | filters.PHOTO | filters.ANIMATION) & filters.ChatType.PRIVATE,
            on_media,
        )
    )
    # Notes are made in private chats only: a group is where they are posted, not where they are asked for.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    return application


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_application(Config.from_env()).run_polling(allowed_updates=Update.ALL_TYPES)
