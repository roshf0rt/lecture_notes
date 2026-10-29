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
from .pipeline import NotesPipeline, Result, Source
from .render_tg import plain_text
from .services import ADDED_OUTCOMES, SHARED, Services, add_keys, check_keys, edit, key_report, reply, services
from .settings import SettingsStore, UserSettings
from .storage import BotState, Options
from .themes import CUSTOM, parse_hex
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
            data.setdefault("batch", []).append(Incoming(message.message_id, Source(text=text), name or "PDF"))
    elif is_text:
        data.setdefault("batch", []).append(Incoming(message.message_id, Source(text=_decode(body)), name or "Текст"))
    elif not message.document or is_media(name, mime) or sniff(body if body is not None else _head(path)):
        # A document without a telling name or type may still be a recording: the header decides.
        source = Source(audio=body, name=name, mime=mime, path=path)
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
            await _make_note(message, status, s, user_id, model, source, sources, ui, options, raw)
    finally:
        s.queued[user_id] -= 1
        if s.queued[user_id] <= 0:
            s.queued.pop(user_id, None)
        s.publish_active()


async def _make_note(message, status, s, user_id, model, source, sources, ui, options, raw=False) -> None:
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
            result = await NotesPipeline(model).run(
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
    await _status(status, f"{ui.e('done')} Конспект готов" + (f" ({', '.join(notes)})" if notes else ""))
    await _send(message, result)


async def _status(status: Message, text: str) -> None:
    with suppress(TelegramError):  # a lost status line must not stop the job
        await edit(status, text)


def _transcript_name(sources) -> str:
    names = [Path(item.name).stem for item in sources if item.is_audio and item.name]
    return f"Расшифровка — {_file_name(names[0])}.txt" if len(names) == 1 else "Расшифровка.txt"


def _file_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()
    return (cleaned or "Конспект")[:80]


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
    application.add_handler(CallbackQueryHandler(admin.on_admin_button, pattern=r"^ad:"))
    application.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL
            | filters.PHOTO | filters.ANIMATION,
            on_media,
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_application(Config.from_env()).run_polling(allowed_updates=Update.ALL_TYPES)
