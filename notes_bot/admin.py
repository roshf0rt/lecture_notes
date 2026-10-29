"""Admin panel in Telegram: statistics, users, access, model, shared keys, broadcast, options."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from .keys import KEY_PATTERN, POOL
from .menus import confirm_clear, key_prompt, keys_view
from .services import Services, add_keys, check_keys, edit, key_report, reply, services
from .storage import ACCESS_MODES, DAILY_LIMITS, UserRecord
from .ui import Ui, html

log = logging.getLogger(__name__)

PAGE_SIZE = 8
UPDATE_REQUEST = "update.request"  # read by deploy/update.sh on the server
BROADCAST_DELAY = 0.05  # stays well under Telegram's ~30 messages per second
MODEL_PRESETS = (
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
)
ACCESS_TITLES = {"open": "Открыт для всех", "whitelist": "Только по списку"}
# Awaited replies from an admin, kept in user_data["awaiting"].
AWAIT_WHITELIST, AWAIT_FIND, AWAIT_MODEL, AWAIT_POOL, AWAIT_BROADCAST = (
    "admin:whitelist", "admin:find", "admin:model", "admin:pool", "admin:broadcast",
)


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _on(value: bool) -> str:
    return "вкл" if value else "выкл"


def _back(ui: Ui, data: str = "ad:home"):
    return ui.button("back", "Назад", data)


# --- views ------------------------------------------------------------------------------


def home_view(ui: Ui, s: Services) -> tuple[str, InlineKeyboardMarkup]:
    stats = s.state.stats()
    options = s.options()
    text = "\n".join(
        [
            f"{ui.e('admin')} <b>Админ-панель</b>",
            "",
            f"{ui.e('users')} Пользователей: <b>{stats.users}</b>, за сутки активны {stats.active_day}",
            f"{ui.e('study')} Конспектов: <b>{stats.notes_total}</b>, за сутки {stats.notes_day}",
            f"{ui.e('model')} Модель: <code>{html(options.model)}</code>",
            f"{ui.e('unlocked' if options.access == 'open' else 'locked')} Доступ: {ACCESS_TITLES[options.access]}",
            f"{ui.e('key')} Общие ключи: {s.keys.count(POOL)}, раздача {_on(options.shared_keys)}",
            f"{ui.e('clip')} Файлы: до {'2 ГБ (локальный Bot API)' if s.config.local_mode else '20 МБ (облачный Bot API)'}",
        ]
        + ([f"{ui.e('sleep')} <b>Включено обслуживание:</b> бот отвечает только админам."] if options.maintenance else [])
    )
    rows = [
        [ui.button("stats", "Статистика", "ad:stats"), ui.button("users", "Пользователи", "ad:users:0")],
        [ui.button("locked", "Доступ", "ad:access"), ui.button("model", "Модель", "ad:model")],
        [ui.button("key", "Общие ключи", "ad:pool:show"), ui.button("broadcast", "Рассылка", "ad:bc")],
        [ui.button("switches", "Параметры", "ad:opt"), ui.button("search", "Найти по ID", "ad:find")],
        [ui.button("reset", "Обновить", "ad:home"), ui.button("no", "Закрыть", "ad:close")],
        [ui.button("reset", "Обновить бота из git", "ad:update", style="primary")],
    ]
    return text, InlineKeyboardMarkup(rows)


def stats_view(ui: Ui, s: Services) -> tuple[str, InlineKeyboardMarkup]:
    stats = s.state.stats()
    lines = [
        f"{ui.e('stats')} <b>Статистика</b>",
        "",
        f"Пользователей: <b>{stats.users}</b> (за сутки {stats.active_day}, за неделю {stats.active_week}, заблокировано {stats.banned})",
        f"С ключами Gemini: {s.keys.owners_with_keys()}",
        f"Конспектов всего: <b>{stats.notes_total}</b>, за сутки {stats.notes_day}",
        f"Ошибок за сутки: {stats.failures_day}",
        f"Из записей: {round(stats.audio_share * 100)}%, среднее время {stats.average_seconds:.0f} с",
    ]
    top = s.state.top_users()
    if top:
        lines += ["", f"{ui.e('star')} <b>Самые активные</b>"]
        lines += [f"{n}. {html(record.label)} — {count}" for n, (record, count) in enumerate(top, start=1)]
    errors = s.state.recent_errors()
    if errors:
        lines += ["", f"{ui.e('warning')} <b>Последние ошибки</b>"]
        lines += [f"{_when(at)} · <code>{user_id}</code> · {html(error[:120])}" for at, user_id, error in errors]
    rows = [[ui.button("reset", "Обновить", "ad:stats"), _back(ui)]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def users_view(ui: Ui, s: Services, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = s.state.user_count()
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    records = s.state.users(page * PAGE_SIZE, PAGE_SIZE)
    text = f"{ui.e('users')} <b>Пользователи</b>: {total}. Страница {page + 1} из {pages}, сначала недавние."
    rows = []
    for record in records:
        icon = "ban" if record.banned else ("admin" if s.is_admin(record.user_id) else "user")
        rows.append([ui.button(icon, f"{record.label} · {record.notes}", f"ad:user:{record.user_id}")])
    nav = []
    if page > 0:
        nav.append(ui.button("back", "Пред.", f"ad:users:{page - 1}"))
    if page < pages - 1:
        nav.append(ui.button("next", "След.", f"ad:users:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_back(ui)])
    return text, InlineKeyboardMarkup(rows)


def user_view(ui: Ui, s: Services, user_id: int, viewer: int) -> tuple[str, InlineKeyboardMarkup]:
    record = s.state.user(user_id) or UserRecord(user_id, "", "", 0, 0, False, 0)
    settings = s.store.get(user_id)
    is_admin = s.is_admin(user_id)
    whitelisted = s.state.is_whitelisted(user_id)
    lines = [
        f"{ui.e('user')} <b>{html(record.full_name or record.label)}</b>",
        f"ID: <code>{user_id}</code>" + (f" · @{html(record.username)}" if record.username else ""),
    ]
    if record.first_seen:
        lines += [f"Впервые: {_when(record.first_seen)}", f"Последний раз: {_when(record.last_seen)}"]
    else:
        lines.append("Ещё не писал боту.")
    lines += [
        f"Конспектов: {record.notes}, за сутки {s.state.notes_last_day(user_id)}",
        f"Ключей Gemini: {s.keys.count(user_id)}",
        f"Формат: {settings.output}, длина: {settings.length}",
        "Роль: " + ("админ" + (" (из bot.env)" if user_id in s.state.env_admins else "") if is_admin else "пользователь"),
        "Статус: " + ("заблокирован" if record.banned else "активен") + (", в белом списке" if whitelisted else ""),
    ]
    rows = []
    if user_id != viewer and user_id not in s.state.env_admins:
        rows.append([
            ui.button("ok", "Разблокировать", f"ad:ban:{user_id}:0", style="success") if record.banned
            else ui.button("ban", "Заблокировать", f"ad:ban:{user_id}:1", style="danger"),
            ui.button("admin", "Снять админа" if is_admin else "Сделать админом", f"ad:adm:{user_id}:{0 if is_admin else 1}"),
        ])
    rows.append([ui.button("shield", "Убрать из списка" if whitelisted else "В белый список",
                           f"ad:wl:{user_id}:{0 if whitelisted else 1}")])
    rows.append([ui.button("back", "К списку", "ad:users:0"), ui.button("admin", "Меню", "ad:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def access_view(ui: Ui, s: Services) -> tuple[str, InlineKeyboardMarkup]:
    options = s.options()
    whitelist = s.state.whitelist()
    lines = [
        f"{ui.e('locked')} <b>Доступ</b>",
        "",
        "«Открыт для всех» — пользоваться может любой, у кого есть ключ Gemini. "
        "«Только по списку» — только люди из белого списка и админы.",
        "",
        f"Белый список: {len(whitelist)}",
    ]
    lines += [f"• <code>{user_id}</code>" for user_id in whitelist[:30]]
    rows = [[ui.button("ok" if options.access == mode else None, ACCESS_TITLES[mode], f"ad:access:{mode}",
                       style="success" if options.access == mode else None)] for mode in ACCESS_MODES]
    rows += [[ui.button("trash", f"Убрать {user_id}", f"ad:wl:{user_id}:0:access")] for user_id in whitelist[:20]]
    rows.append([ui.button("add", "Добавить ID", "ad:wladd", style="primary"), _back(ui)])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def model_view(ui: Ui, s: Services, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    current = s.options().model
    text = (f"{ui.e('model')} <b>Модель Gemini</b>\n\nСейчас: <code>{html(current)}</code>\n\n"
            "Модель используется для расшифровки и конспекта у всех пользователей. Лимиты бесплатного плана "
            "у моделей разные: смотрите их в AI Studio.")
    if note:
        text += f"\n\n{note}"
    rows = [[ui.button("ok" if name == current else None, name, f"ad:model:set:{index}",
                       style="success" if name == current else None)]
            for index, name in enumerate(MODEL_PRESETS)]
    rows.append([ui.button("writing", "Своя модель", "ad:model:custom"), ui.button("search", "Проверить", "ad:model:test")])
    rows.append([_back(ui)])
    return text, InlineKeyboardMarkup(rows)


def pool_view(ui: Ui, s: Services, statuses: dict[int, str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    text, keyboard = keys_view(ui, s.keys.list(POOL), s.ring, s.keys.limit(POOL), prefix="ad:pool",
                               title="Общие ключи", back="ad:home", statuses=statuses)
    shared = s.options().shared_keys
    text += ("\n\nОбщие ключи получают пользователи, у которых нет своего ключа. "
             f"Сейчас раздача <b>{_on(shared)}</b>. Все запросы этих пользователей тратят ваши квоты.")
    rows = list(keyboard.inline_keyboard)
    rows.insert(-1, [ui.button("ok" if shared else "no", f"Раздача пользователям: {_on(shared)}", "ad:pool:toggle")])
    return text, InlineKeyboardMarkup(rows)


def options_view(ui: Ui, s: Services) -> tuple[str, InlineKeyboardMarkup]:
    options = s.options()
    limit = f"{options.daily_limit} в сутки" if options.daily_limit else "без лимита"
    text = "\n".join(
        [
            f"{ui.e('switches')} <b>Параметры</b>",
            "",
            f"{ui.e('star')} Премиум-эмодзи: <b>{_on(options.premium_emoji)}</b>. Нужен Telegram Premium у владельца бота, "
            "иначе вместо иконок будут обычные эмодзи. Если иконки в кнопках пропали, выключите.",
            f"{ui.e('speed')} Лимит конспектов на человека: <b>{limit}</b>. На админов не действует.",
            f"{ui.e('bell')} Уведомлять админов о новых пользователях: <b>{_on(options.notify_new_users)}</b>",
            f"{ui.e('sleep')} Обслуживание: <b>{_on(options.maintenance)}</b>. Пока включено, бот отвечает только админам.",
        ]
    )
    rows = [
        [ui.button("star", f"Премиум-эмодзи: {_on(options.premium_emoji)}", "ad:opt:premium_emoji")],
        [ui.button("speed", f"Лимит: {limit}", "ad:opt:daily_limit")],
        [ui.button("bell", f"Новые пользователи: {_on(options.notify_new_users)}", "ad:opt:notify_new_users")],
        [ui.button("sleep", f"Обслуживание: {_on(options.maintenance)}", "ad:opt:maintenance",
                   style="danger" if options.maintenance else None)],
        [_back(ui)],
    ]
    return text, InlineKeyboardMarkup(rows)


def prompt_view(ui: Ui, text: str, back: str = "ad:home") -> tuple[str, InlineKeyboardMarkup]:
    return text, InlineKeyboardMarkup([[ui.button("back", "Отмена", back)]])


def not_admin_text(user_id: int, has_admins: bool) -> str:
    if has_admins:
        return "Админ-панель доступна только админам бота."
    return (
        "Админов пока нет. Чтобы стать админом, выполните на сервере:\n\n"
        f"<code>echo 'ADMIN_IDS={user_id}' &gt;&gt; /etc/notes-bot/bot.env &amp;&amp; systemctl restart notes-bot</code>\n\n"
        f"Ваш ID: <code>{user_id}</code>"
    )


# --- handlers ---------------------------------------------------------------------------


async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    user_id = update.effective_user.id
    if not s.is_admin(user_id):
        await reply(update.effective_message, not_admin_text(user_id, bool(s.state.admins())))
        return
    context.user_data.pop("awaiting", None)
    await reply(update.effective_message, *home_view(s.ui(), s))


async def on_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = services(context)
    viewer = query.from_user.id
    if not s.is_admin(viewer):
        await query.answer("Только для админов", show_alert=True)
        return
    _, action, *args = query.data.split(":")
    ui = s.ui()
    context.user_data.pop("awaiting", None)

    if action == "close":
        await query.answer()
        await edit(query, f"{ui.e('admin')} Админ-панель закрыта. Открыть снова: /admin")
        return
    if action == "update":
        # The bot cannot replace its own code (it runs without root): it leaves a request, and a root
        # service on the server does the update and tells the admin how it went.
        (s.config.data_dir / UPDATE_REQUEST).write_text(str(viewer), encoding="utf-8")
        await query.answer("Запрос отправлен")
        await edit(query, f"{ui.e('reset')} Обновление запрошено. Если есть новый код, бот обновится и перезапустится "
                           "в течение пары минут. Сообщение придёт сюда.", InlineKeyboardMarkup(
                               [[ui.button("back", "Назад", "ad:home")]]))
        return
    if action == "stats":
        await query.answer()
        await edit(query, *stats_view(ui, s))
        return
    if action == "users":
        await query.answer()
        await edit(query, *users_view(ui, s, int(args[0])))
        return
    if action == "user":
        await query.answer()
        await edit(query, *user_view(ui, s, int(args[0]), viewer))
        return
    if action in ("ban", "adm", "wl"):
        user_id, value = int(args[0]), args[1] == "1"
        if action != "wl" and (user_id == viewer or user_id in s.state.env_admins):
            await query.answer("Этого пользователя так не изменить", show_alert=True)
            return
        if action == "ban":
            s.state.set_banned(user_id, value)
            await query.answer("Заблокирован" if value else "Разблокирован")
        elif action == "adm":
            s.state.set_admin(user_id, value)
            await query.answer("Теперь админ" if value else "Больше не админ")
        else:
            s.state.set_whitelisted(user_id, value)
            await query.answer("Добавлен в список" if value else "Убран из списка")
            if args[2:] == ["access"]:
                await edit(query, *access_view(ui, s))
                return
        await edit(query, *user_view(ui, s, user_id, viewer))
        return
    if action == "access":
        if args and args[0] in ACCESS_MODES:
            s.state.set_option("access", args[0])
        await query.answer()
        await edit(query, *access_view(ui, s))
        return
    if action == "wladd":
        await query.answer()
        context.user_data["awaiting"] = AWAIT_WHITELIST
        await edit(query, *prompt_view(ui, f"{ui.e('shield')} Пришлите Telegram ID (число) или несколько через пробел. "
                                           "Свой ID человек увидит, если напишет боту при закрытом доступе.", "ad:access"))
        return
    if action == "find":
        await query.answer()
        context.user_data["awaiting"] = AWAIT_FIND
        await edit(query, *prompt_view(ui, f"{ui.e('search')} Пришлите Telegram ID пользователя."))
        return
    if action == "model":
        await _model_button(query, context, s, ui, args)
        return
    if action == "pool":
        await _pool_button(query, context, s, ui, args)
        return
    if action == "bc":
        await _broadcast_button(query, context, s, ui, args)
        return
    if action == "opt":
        if args:
            _toggle_option(s, args[0])
        await query.answer()
        await edit(query, *options_view(s.ui(), s))
        return
    await query.answer()
    await edit(query, *home_view(ui, s))


def _toggle_option(s: Services, name: str) -> None:
    options = s.options()
    if name == "daily_limit":
        index = DAILY_LIMITS.index(options.daily_limit) if options.daily_limit in DAILY_LIMITS else 0
        s.state.set_option(name, DAILY_LIMITS[(index + 1) % len(DAILY_LIMITS)])
    elif name in ("premium_emoji", "maintenance", "notify_new_users"):
        s.state.set_option(name, not getattr(options, name))


async def _test_model(s: Services, viewer: int, model: str, ui: Ui) -> tuple[bool, str]:
    """Try the model with the admin's own key, or with a shared one."""
    keys = s.keys.values(viewer) or s.keys.values(POOL)
    if not keys:
        return True, f"{ui.e('info')} Не на чем проверить: добавьте свой ключ или общий."
    try:
        await s.client(keys[0], model).check_key()
    except Exception as error:  # GeminiError and anything unexpected end up in the same note
        return False, f"{ui.e('no')} <code>{html(model)}</code> не ответила: {html(str(error))}"
    return True, f"{ui.e('ok')} <code>{html(model)}</code> отвечает."


async def _model_button(query, context, s: Services, ui: Ui, args: list[str]) -> None:
    if args[:1] == ["set"]:
        index = int(args[1])
        if 0 <= index < len(MODEL_PRESETS):
            s.state.set_option("model", MODEL_PRESETS[index])
        await query.answer("Модель выбрана")
        await edit(query, *model_view(ui, s))
        return
    if args[:1] == ["custom"]:
        await query.answer()
        context.user_data["awaiting"] = AWAIT_MODEL
        await edit(query, *prompt_view(ui, f"{ui.e('model')} Пришлите название модели, например "
                                           "<code>gemini-3.5-flash</code>. Бот проверит её перед сохранением.", "ad:model"))
        return
    if args[:1] == ["test"]:
        await query.answer("Проверяю…")
        _, note = await _test_model(s, query.from_user.id, s.options().model, ui)
        await edit(query, *model_view(ui, s, note))
        return
    await query.answer()
    await edit(query, *model_view(ui, s))


async def _pool_button(query, context, s: Services, ui: Ui, args: list[str]) -> None:
    command = args[0] if args else "show"
    if command == "add":
        await query.answer()
        context.user_data["awaiting"] = AWAIT_POOL
        await edit(query, *key_prompt(ui, "ad:pool:show"))
        return
    if command == "del":
        s.keys.remove(POOL, int(args[1]))
        await query.answer("Ключ удалён")
    elif command == "clear":
        if args[1:] != ["yes"]:
            await query.answer()
            await edit(query, *confirm_clear(ui, "ad:pool"))
            return
        s.keys.clear(POOL)
        await query.answer("Все общие ключи удалены")
    elif command == "toggle":
        s.state.set_option("shared_keys", not s.options().shared_keys)
        await query.answer()
    elif command == "check":
        await query.answer("Проверяю ключи…")
        await edit(query, *pool_view(ui, s, await check_keys(s, POOL)))
        return
    else:
        await query.answer()
    await edit(query, *pool_view(ui, s))


async def _broadcast_button(query, context, s: Services, ui: Ui, args: list[str]) -> None:
    if args[:1] == ["send"]:
        draft = context.user_data.pop("broadcast", None)
        if draft is None:
            await query.answer("Черновик потерялся, начните заново", show_alert=True)
            return
        await query.answer("Рассылка началась")
        recipients = s.state.recipients()
        await edit(query, f"{ui.e('broadcast')} Отправляю {len(recipients)} пользователям…")
        context.application.create_task(_broadcast(context, s, draft, recipients, query.from_user.id))
        return
    if args[:1] == ["cancel"]:
        context.user_data.pop("broadcast", None)
        await query.answer("Отменено")
        await edit(query, *home_view(ui, s))
        return
    await query.answer()
    context.user_data["awaiting"] = AWAIT_BROADCAST
    await edit(query, *prompt_view(ui, f"{ui.e('broadcast')} Пришлите сообщение для рассылки: текст, фото, видео или файл. "
                                       "Оно уйдёт всем, кто не заблокирован, в том же виде."))


async def _broadcast(context, s: Services, draft: tuple[int, int], recipients: list[int], admin_id: int) -> None:
    chat_id, message_id = draft
    sent = failed = 0
    started = time.monotonic()
    for user_id in recipients:
        try:
            await context.bot.copy_message(chat_id=user_id, from_chat_id=chat_id, message_id=message_id)
            sent += 1
        except Forbidden:
            failed += 1  # the user blocked the bot
        except TelegramError as error:
            log.warning("Broadcast to %s failed: %s", user_id, error)
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)
    ui = s.ui()
    text = (f"{ui.e('broadcast')} Рассылка завершена за {time.monotonic() - started:.0f} с.\n"
            f"{ui.e('ok')} Доставлено: {sent}\n{ui.e('no')} Не доставлено: {failed}")
    try:
        await context.bot.send_message(admin_id, text, parse_mode="HTML")
    except TelegramError:
        log.warning("Could not report the broadcast result to %s", admin_id)


async def handle_awaited(update: Update, context: ContextTypes.DEFAULT_TYPE, awaiting: str) -> None:
    """An admin replied to one of the prompts above."""
    s = services(context)
    message = update.effective_message
    viewer = update.effective_user.id
    ui = s.ui()
    if not s.is_admin(viewer):
        context.user_data.pop("awaiting", None)
        return
    if awaiting == AWAIT_BROADCAST:
        context.user_data.pop("awaiting", None)
        context.user_data["broadcast"] = (message.chat_id, message.message_id)
        count = len(s.state.recipients())
        keyboard = InlineKeyboardMarkup([[ui.button("send", f"Отправить {count}", "ad:bc:send", style="success"),
                                          ui.button("no", "Отмена", "ad:bc:cancel", style="danger")]])
        await reply(message, f"{ui.e('broadcast')} Сообщение выше уйдёт {count} пользователям. Отправить?", keyboard)
        return

    text = (message.text or "").strip()
    if awaiting == AWAIT_WHITELIST:
        ids = [int(part) for part in text.replace(",", " ").split() if part.lstrip("-").isdigit()]
        if not ids:
            await reply(message, f"{ui.e('warning')} Не нашёл чисел. Пришлите ID ещё раз.")
            return
        context.user_data.pop("awaiting", None)
        for user_id in ids:
            s.state.set_whitelisted(user_id, True)
        text, keyboard = access_view(ui, s)
        await reply(message, f"{ui.e('ok')} Добавлено в белый список: {len(ids)}.\n\n{text}", keyboard)
        return
    if awaiting == AWAIT_FIND:
        if not text.lstrip("-").isdigit():
            await reply(message, f"{ui.e('warning')} ID — это число. Пришлите ещё раз.")
            return
        context.user_data.pop("awaiting", None)
        await reply(message, *user_view(ui, s, int(text), viewer))
        return
    if awaiting == AWAIT_MODEL:
        name = text.removeprefix("models/")
        if not name or " " in name or len(name) > 80:
            await reply(message, f"{ui.e('warning')} Это не похоже на название модели.")
            return
        context.user_data.pop("awaiting", None)
        status = await reply(message, f"{ui.e('clock')} Проверяю <code>{html(name)}</code>…")
        works, note = await _test_model(s, viewer, name, ui)
        if works:
            s.state.set_option("model", name)
        await edit(status, *model_view(ui, s, note))
        return
    if awaiting == AWAIT_POOL:
        found = KEY_PATTERN.findall(text)
        if not found:
            await reply(message, f"{ui.e('warning')} Не нашёл ключа в сообщении. Ключи Gemini начинаются с AIza или AQ.")
            return
        context.user_data.pop("awaiting", None)
        status = await reply(message, f"{ui.e('clock')} Проверяю ключи…")
        report = await add_keys(s, POOL, found)
        text, keyboard = pool_view(ui, s)
        await edit(status, f"{key_report(ui, report)}\n\n{text}", keyboard)
        return
    context.user_data.pop("awaiting", None)


async def notify_new_user(context: ContextTypes.DEFAULT_TYPE, s: Services, user) -> None:
    ui = s.ui()
    name = html(user.full_name or "")
    handle = f" @{html(user.username)}" if user.username else ""
    text = f"{ui.e('user')} Новый пользователь: {name}{handle} · <code>{user.id}</code>"
    keyboard = InlineKeyboardMarkup([[ui.button("user", "Открыть", f"ad:user:{user.id}")]])
    for admin_id in s.state.admins():
        try:
            await context.bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
        except TelegramError:
            log.warning("Could not notify admin %s", admin_id)
