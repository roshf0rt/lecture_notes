from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from contextlib import suppress
from dataclasses import replace

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Message, Update
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
from .gemini import GeminiClient, GeminiError
from .keys import KEY_PATTERN, KeyVault
from .menus import (
    AI_STUDIO,
    BLOCK_PRESETS,
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
from .pipeline import NotesPipeline, Result
from .render_tg import plain_text
from .services import ADDED, SHARED, Services, add_keys, check_keys, edit, key_report, reply, services
from .settings import SettingsStore, UserSettings
from .storage import BotState, Options
from .themes import CUSTOM, parse_hex
from .ui import Ui, html

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024  # Bot API refuses to download bigger files
TEXT_EXTENSIONS = (".txt", ".md")
STAGES = {
    "queued": ("clock", "Жду свободного места, конспектов сейчас много…"),
    "transcribing": ("transcribing", "Расшифровываю запись…"),
    "writing": ("writing", "Пишу конспект…"),
    "rendering": ("rendering", "Оформляю…"),
}
AWAIT_KEY, AWAIT_COLOR = "key", "color"
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
        welcome(ui, s.keys.count(user_id) > 0, s.options().shared_keys),
        main_keyboard(ui, s.is_admin(user_id)),
    )
    if s.is_admin(user_id):
        with suppress(TelegramError):
            await context.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(update.effective_chat.id))


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    ui = s.ui()
    await reply(update.effective_message, help_text(ui), main_keyboard(ui, s.is_admin(update.effective_user.id)))


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
    elif action == "blk":
        settings = settings.toggle_block(args[0])
        view = "blocks"
    elif action == "preset" and args[0] in BLOCK_PRESETS:
        settings = replace(settings, blocks=BLOCK_PRESETS[args[0]])
        view = "blocks"
    elif action == "tr":
        settings = replace(settings, send_transcript=not settings.send_transcript)
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
    added = any(outcome == ADDED for _, outcome in report)
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
    await _run_job(update, context, text=text)


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


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


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
    attachment, name, mime = found
    if (attachment.file_size or 0) > MAX_FILE_BYTES:
        await reply(message, f"{ui.e('warning')} Файл больше 20 МБ, Telegram не даёт боту его скачать. "
                             "Сожмите запись или разбейте её на части.")
        return

    file = await attachment.get_file()
    data = bytes(await file.download_as_bytearray())
    if (name or "").lower().endswith(TEXT_EXTENSIONS) or (mime or "").startswith("text/plain"):
        await _run_job(update, context, text=_decode(data))
    elif not message.document or is_media(name, mime) or sniff(data):
        # A document without a telling name or type may still be a recording: the header decides.
        await _run_job(update, context, audio=data, audio_name=name, audio_mime=mime)
    else:
        await reply(message, f"{ui.e('warning')} Не понимаю этот файл. Пришлите аудио, видео или текст в формате .txt.")


# --- jobs -------------------------------------------------------------------------------


def no_key_text(ui: Ui) -> str:
    return (f"{ui.e('key')} Чтобы делать конспекты, нужен ключ Gemini. Он бесплатный: создайте его в "
            f'<a href="{AI_STUDIO}">Google AI Studio</a> и пришлите сюда одним сообщением.')


def _stage(ui: Ui, stage: str) -> str:
    icon, text = STAGES[stage]
    return f"{ui.e(icon)} {text}"


async def _run_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str | None = None,
    audio: bytes | None = None,
    audio_name: str | None = None,
    audio_mime: str | None = None,
) -> None:
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

    lock = s.locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await reply(message, f"{ui.e('clock')} Предыдущий конспект ещё готовится. Дождитесь, пожалуйста, его.")
        return

    async with lock:
        settings = s.store.get(user_id)
        start_stage = "transcribing" if audio is not None else "writing"
        status = await reply(message, _stage(ui, "queued" if s.slots.locked() else start_stage))

        async def progress(stage: str) -> None:
            await _status(status, _stage(ui, stage))

        kind = "audio" if audio is not None else "text"
        started = time.monotonic()
        async with s.slots:
            try:
                result = await NotesPipeline(model).run(
                    settings, progress=progress, text=text, audio=audio, audio_name=audio_name,
                    audio_mime=audio_mime, premium=options.premium_emoji,
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
        await _status(status, f"{ui.e('done')} Конспект готов" + (" (на общем ключе)" if source == SHARED else ""))
        await _send(message, result, settings, audio is not None)


async def _status(status: Message, text: str) -> None:
    with suppress(TelegramError):  # a lost status line must not stop the job
        await edit(status, text)


def _file_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()
    return (cleaned or "Конспект")[:80]


async def _send(message: Message, result: Result, settings: UserSettings, from_audio: bool) -> None:
    name = _file_name(result.document.title)
    if result.pdf is not None:
        await message.reply_document(document=io.BytesIO(result.pdf), filename=f"{name}.pdf")
    else:
        for text in result.messages or []:
            try:
                await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest:
                log.warning("Telegram rejected a MarkdownV2 message; sending it as plain text")
                await message.reply_text(plain_text(text))
    if settings.send_transcript and from_audio:
        await message.reply_document(
            document=io.BytesIO(result.transcript.encode("utf-8")), filename=f"{name} — расшифровка.txt"
        )


# --- application ------------------------------------------------------------------------


async def _post_init(application: Application) -> None:
    s: Services = application.bot_data["services"]
    try:
        await application.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    except TelegramError:
        log.warning("Could not set the bot command menu")
        return
    for admin_id in s.state.admins():
        with suppress(TelegramError):  # an admin who never opened the bot has no chat yet
            await application.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(admin_id))


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
        make_client=lambda api_key, model: GeminiClient(api_key, model),
    )


def build_application(config: Config) -> Application:
    application = (
        Application.builder().token(config.telegram_token).concurrent_updates(True).post_init(_post_init).build()
    )
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
