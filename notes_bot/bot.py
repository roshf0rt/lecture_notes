from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace

from telegram import InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .gemini import GeminiClient, GeminiError, is_media_name
from .keys import KeyVault
from .menus import apply_choice, build_view, color_prompt
from .pipeline import Model, NotesPipeline, Result
from .render_tg import plain_text
from .settings import SettingsStore
from .themes import CUSTOM, parse_hex

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024  # Bot API refuses to download bigger files
TEXT_EXTENSIONS = (".txt", ".md")
KEY_PATTERN = re.compile(r"(?:AIza|AQ\.)[0-9A-Za-z_.-]{30,80}")
STAGES = {
    "transcribing": "🎙 Расшифровываю запись…",
    "writing": "✍️ Пишу конспект…",
    "rendering": "🎨 Оформляю…",
}
WELCOME = (
    "Привет! Я делаю конспекты лекций в аккуратном оформлении.\n\n"
    "Сначала пришлите свой ключ Gemini: команда /setkey, затем ключ одним сообщением. "
    "Бесплатный ключ создаётся в Google AI Studio: https://aistudio.google.com/apikey\n\n"
    "Потом пришлите:\n"
    "• голосовое, аудио или видео с речью (до 20 МБ);\n"
    "• текст расшифровки сообщением или файлом .txt.\n\n"
    "Формат, длину, блоки и цвета настраивают в /settings."
)
NO_KEY = (
    "Чтобы делать конспекты, сначала пришлите свой ключ Gemini: команда /setkey.\n"
    "Бесплатный ключ создаётся в Google AI Studio: https://aistudio.google.com/apikey"
)
KEY_SAVED = (
    "✅ Ключ сохранён в зашифрованном виде. Теперь пришлите запись или текст лекции.\n"
    "Удалите сообщение с ключом из чата: бот не может сделать это сам."
)


@dataclass
class Services:
    config: Config
    store: SettingsStore
    keys: KeyVault
    make_model: Callable[[str], Model]
    locks: dict[int, asyncio.Lock]


def _services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


async def _allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    allowed = _services(context).config.allowed_user_ids
    if user is not None and (not allowed or user.id in allowed):
        return True
    if update.callback_query is not None:
        await update.callback_query.answer("Доступ закрыт", show_alert=True)
    elif update.effective_message is not None:
        who = f" Ваш ID: {user.id}." if user is not None else ""
        await update.effective_message.reply_text(f"Доступ закрыт.{who} Попросите владельца бота добавить вас.")
    return False


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _allowed(update, context):
        await update.effective_message.reply_text(WELCOME)


async def on_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    context.user_data["awaiting"] = "key"
    await update.effective_message.reply_text(
        "Пришлите ключ Gemini одним сообщением.\n"
        "Получить ключ: https://aistudio.google.com/apikey"
    )


async def on_forgetkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    _services(context).keys.delete(update.effective_user.id)
    await update.effective_message.reply_text("Ключ удалён. Чтобы снова делать конспекты, пришлите новый: /setkey.")


async def on_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    settings = _services(context).store.get(update.effective_user.id)
    text, keyboard = build_view("main", settings)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _edit(query, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    with suppress(BadRequest):  # "message is not modified" when a tap changes nothing
        await query.edit_message_text(text, reply_markup=keyboard)


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _allowed(update, context):
        return
    await query.answer()

    services = _services(context)
    user_id = query.from_user.id
    settings = services.store.get(user_id)
    _, action, *args = query.data.split(":")

    if action == "color":
        context.user_data["awaiting"] = "color"
        await _edit(query, *color_prompt())
        return
    if action == "close":
        await _edit(query, "Настройки сохранены. Пришлите запись или текст, и я сделаю конспект.", None)
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
    services.store.save(user_id, settings)
    await _edit(query, *build_view(view, settings))


async def _save_color(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    color = parse_hex(raw)
    if color is None:
        context.user_data["awaiting"] = "color"
        await update.effective_message.reply_text("Не понял цвет или он слишком светлый. Пример: #3B82F6")
        return
    services = _services(context)
    user_id = update.effective_user.id
    settings = replace(services.store.get(user_id), theme=CUSTOM, custom_color=color)
    services.store.save(user_id, settings)
    text, keyboard = build_view("theme", settings)
    await update.effective_message.reply_text(f"Цвет {color} сохранён.\n\n{text}", reply_markup=keyboard)


async def _save_key(update: Update, context: ContextTypes.DEFAULT_TYPE, api_key: str) -> None:
    message = update.effective_message
    services = _services(context)
    try:
        await services.make_model(api_key).check_key()
    except GeminiError as error:
        await message.reply_text(f"😕 {error}\nПришлите другой ключ.")
        return
    except Exception:
        log.exception("Gemini key check failed unexpectedly")
        await message.reply_text("😕 Не получилось проверить ключ. Попробуйте ещё раз чуть позже.")
        return
    services.keys.save(update.effective_user.id, api_key)
    context.user_data.pop("awaiting", None)
    await message.reply_text(KEY_SAVED)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    text = update.effective_message.text
    awaiting = context.user_data.get("awaiting")
    if awaiting == "key":
        await _save_key(update, context, text.strip())
        return
    if awaiting == "color":
        context.user_data.pop("awaiting", None)
        await _save_color(update, context, text)
        return
    if KEY_PATTERN.fullmatch(text.strip()):
        await _save_key(update, context, text.strip())
        return
    await _run_job(update, context, text=text)


def _attachment(message: Message) -> tuple[object, str] | None:
    if message.voice:
        return message.voice, "voice.ogg"
    if message.audio:
        return message.audio, message.audio.file_name or "audio.mp3"
    if message.video_note:
        return message.video_note, "video_note.mp4"
    if message.video:
        return message.video, "video.mp4"
    if message.document:
        return message.document, message.document.file_name or "document"
    return None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    message = update.effective_message
    found = _attachment(message)
    if found is None:
        return
    attachment, name = found
    lowered = name.lower()
    if not (lowered.endswith(TEXT_EXTENSIONS) or is_media_name(name) or not message.document):
        await message.reply_text("Не понимаю этот файл. Пришлите аудио, видео или текст в формате .txt.")
        return
    if (attachment.file_size or 0) > MAX_FILE_BYTES:
        await message.reply_text("Файл больше 20 МБ, Telegram не даёт боту его скачать. Сожмите запись или разбейте её на части.")
        return

    file = await attachment.get_file()
    data = bytes(await file.download_as_bytearray())
    if lowered.endswith(TEXT_EXTENSIONS):
        await _run_job(update, context, text=_decode(data))
    else:
        await _run_job(update, context, audio=data, audio_name=name)


async def _run_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str | None = None,
    audio: bytes | None = None,
    audio_name: str | None = None,
) -> None:
    message = update.effective_message
    user_id = update.effective_user.id
    services = _services(context)
    api_key = services.keys.load(user_id)
    if api_key is None:
        await message.reply_text(NO_KEY)
        return

    lock = services.locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await message.reply_text("Предыдущий конспект ещё готовится. Дождитесь, пожалуйста, его.")
        return

    async with lock:
        settings = services.store.get(user_id)
        status = await message.reply_text(STAGES["transcribing" if audio is not None else "writing"])

        async def progress(stage: str) -> None:
            with suppress(TelegramError):
                await status.edit_text(STAGES[stage])

        pipeline = NotesPipeline(services.make_model(api_key))
        try:
            result = await pipeline.run(settings, progress=progress, text=text, audio=audio, audio_name=audio_name)
        except GeminiError as error:
            with suppress(TelegramError):
                await status.edit_text(f"😕 {error}")
            return
        except Exception:
            log.exception("Failed to build notes for user %s", user_id)
            with suppress(TelegramError):
                await status.edit_text("😕 Не получилось сделать конспект. Попробуйте ещё раз чуть позже.")
            return

        with suppress(TelegramError):
            await status.edit_text("✅ Конспект готов")
        await _send(message, result)


def _file_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()
    return (cleaned or "Конспект")[:80]


async def _send(message: Message, result: Result) -> None:
    if result.pdf is not None:
        await message.reply_document(
            document=io.BytesIO(result.pdf),
            filename=f"{_file_name(result.document.title)}.pdf",
        )
        return
    for text in result.messages or []:
        try:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest:
            log.warning("Telegram rejected a MarkdownV2 message; sending it as plain text")
            await message.reply_text(plain_text(text))


def build_application(config: Config) -> Application:
    application = Application.builder().token(config.telegram_token).concurrent_updates(True).build()

    def make_model(api_key: str) -> GeminiClient:
        return GeminiClient(api_key, config.gemini_model)

    application.bot_data["services"] = Services(
        config=config,
        store=SettingsStore(config.data_dir / "settings.sqlite3"),
        keys=KeyVault(config.data_dir),
        make_model=make_model,
        locks={},
    )
    application.add_handler(CommandHandler(["start", "help"], on_start))
    application.add_handler(CommandHandler("setkey", on_setkey))
    application.add_handler(CommandHandler("forgetkey", on_forgetkey))
    application.add_handler(CommandHandler("settings", on_settings))
    application.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^st:"))
    application.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL,
            on_media,
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_application(Config.from_env()).run_polling(allowed_updates=Update.ALL_TYPES)
