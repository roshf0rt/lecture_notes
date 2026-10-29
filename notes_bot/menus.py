"""Texts and keyboards for users: main menu, settings and Gemini keys. Texts are HTML."""
from __future__ import annotations

from dataclasses import replace

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

from .blocks import EXTRAS, LENGTH_BY_ID, LENGTHS
from .keyring import KeyRing
from .keys import StoredKey
from .settings import LANGUAGES, OUTPUTS, UserSettings
from .themes import CUSTOM, THEMES
from .ui import Ui, html

OUTPUT_TITLES = {
    "both": "PDF и полный текст в чате",
    "both_one": "PDF и одно короткое сообщение",
    "pdf": "Только PDF",
    "tg": "Только полный текст в чате",
    "tg_one": "Только одно короткое сообщение",
}
OUTPUT_ICONS = {"both": "stack", "both_one": "clip", "pdf": "pdf", "tg": "message", "tg_one": "clip"}
LANGUAGE_TITLES = {"auto": "Как в материале", "ru": "Всегда по-русски"}
AI_STUDIO = "https://aistudio.google.com/apikey"

# Reply keyboard labels. A press arrives as plain text, with or without a leading emoji.
MENU_SETTINGS = "Настройки"
MENU_KEYS = "Ключи Gemini"
MENU_HELP = "Помощь"
MENU_ADMIN = "Админка"
MENU_LABELS = (MENU_SETTINGS, MENU_KEYS, MENU_HELP, MENU_ADMIN)


def menu_label(text: str) -> str | None:
    """Which reply keyboard button was pressed, if any."""
    stripped = text.strip()
    for label in MENU_LABELS:
        prefix = stripped.removesuffix(label)
        # Without premium icons the button text starts with an emoji; letters there mean ordinary text.
        if prefix != stripped and len(prefix) <= 4 and not any(char.isalnum() for char in prefix):
            return label
    return None


def main_keyboard(ui: Ui, is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [ui.key("settings", MENU_SETTINGS), ui.key("key", MENU_KEYS)],
        [ui.key("help", MENU_HELP)],
    ]
    if is_admin:
        rows[1].append(ui.key("admin", MENU_ADMIN))
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Пришлите запись или текст лекции")


def size_limit(local_api: bool) -> str:
    """How big a file the bot can take, as the user reads it."""
    return "до 2 ГБ" if local_api else "до 20 МБ"


def welcome(ui: Ui, has_key: bool, shared: bool, local_api: bool = False) -> str:
    lines = [
        f"{ui.e('hello')} <b>Привет! Я делаю конспекты лекций.</b>",
        "",
        f"{ui.e('mic')} Пришлите голосовое, аудио или видео ({size_limit(local_api)}, любой частый формат: ogg, mp3, aac, m4a, wav, mp4…)",
        f"{ui.e('transcript')} или текст расшифровки сообщением либо файлом .txt.",
        "",
        f"{ui.e('settings')} В настройках выбираются формат (PDF или сообщения), длина, язык, цвета и блоки конспекта.",
    ]
    if not has_key:
        lines += ["", f"{ui.e('key')} <b>Сначала нужен ключ Gemini.</b> Он бесплатный: создайте его в "
                      f'<a href="{AI_STUDIO}">Google AI Studio</a> и пришлите сюда одним сообщением.']
        if shared:
            lines.append("Пока ключа нет, бот работает на общих ключах владельца.")
    return "\n".join(lines)


def help_text(ui: Ui, local_api: bool = False) -> str:
    return "\n".join(
        [
            f"{ui.e('help')} <b>Как пользоваться</b>",
            "",
            f"{ui.e('mic')} <b>Запись.</b> Голосовое, аудио, видео или файл {size_limit(local_api)}. Формат определяется по содержимому, "
            "поэтому подойдут и файлы без расширения. Видео и редкие форматы бот сам переводит в аудио.",
            f"{ui.e('transcript')} <b>Текст.</b> Сообщением или файлом .txt, от 100 знаков.",
            f"{ui.e('blocks')} <b>Семестр.</b> Перед текстом или подписью к файлу напишите <code>/semester</code>: бот разобьёт материал по темам и сделает конспект на каждую. Длинные тексты (больше 150 000 знаков) он делит так сам.",
            f"{ui.e('clip')} <b>Группа с разделами.</b> Внутри раздела напишите <code>/bind Физика</code>, и конспекты по физике можно отправлять в группу одной кнопкой. Список разделов: /topics.",
            f"{ui.e('key')} <b>Ключи.</b> Можно добавить несколько ключей Gemini: когда у одного кончается лимит, "
            "бот переходит на следующий. Квота общая на проект Google, поэтому ключи нужны из разных проектов или аккаунтов.",
            f"{ui.e('settings')} <b>Настройки.</b> Формат, длина, язык, оформление PDF.",
            "",
            "Команды: /settings, /keys, /help.",
        ]
    )


# --- settings ---------------------------------------------------------------------------


def _theme_name(settings: UserSettings) -> str:
    if settings.theme == CUSTOM and settings.custom_color:
        return f"свой {settings.custom_color}"
    return THEMES[settings.theme].title


def apply_choice(settings: UserSettings, field: str, value: str) -> UserSettings:
    if field == "output" and value in OUTPUTS:
        return replace(settings, output=value)
    if field == "length" and value in LENGTH_BY_ID:
        return replace(settings, length=value)
    if field == "theme" and value in THEMES:
        return replace(settings, theme=value)
    if field == "language" and value in LANGUAGES:
        return replace(settings, language=value)
    return settings


def _choice(ui: Ui, selected: bool, icon: str | None, text: str, data: str):
    return ui.button("ok" if selected else icon, text, data, style="success" if selected else None)


def _extras_text(settings: UserSettings) -> str:
    chosen = [extra.title for extra in EXTRAS if extra.id in settings.extras]
    return html(", ".join(chosen)) if chosen else "нет"


def settings_view(ui: Ui, name: str, settings: UserSettings) -> tuple[str, InlineKeyboardMarkup]:
    back = ui.button("back", "Назад", "st:menu:main")

    if name == "output":
        rows = [[_choice(ui, settings.output == key, OUTPUT_ICONS[key], title, f"st:set:output:{key}")]
                for key, title in OUTPUT_TITLES.items()]
        text = (f"{ui.e('format')} <b>Как присылать конспект</b>\n\n"
                "<b>Полный текст в чате</b>: конспект целиком, несколько сообщений подряд. "
                "<b>Одно короткое сообщение</b>: выжимка, которая помещается в одно сообщение Telegram. "
                "С PDF короткое сообщение идёт как краткое содержание, а полный конспект лежит в файле.")
        return text, InlineKeyboardMarkup([*rows, [back]])

    if name == "length":
        rows = [[_choice(ui, settings.length == length.id, None, length.title, f"st:set:length:{length.id}")]
                for length in LENGTHS]
        text = f"{ui.e('length')} <b>Длина конспекта</b>\n\n{html(LENGTH_BY_ID[settings.length].guidance)}"
        return text, InlineKeyboardMarkup([*rows, [back]])

    if name == "language":
        rows = [[_choice(ui, settings.language == key, None, title, f"st:set:language:{key}")]
                for key, title in LANGUAGE_TITLES.items()]
        text = (f"{ui.e('language')} <b>Язык конспекта</b>\n\n"
                "«Как в материале» — конспект на языке лекции. «Всегда по-русски» — перевод, "
                "если лекция на другом языке.")
        return text, InlineKeyboardMarkup([*rows, [back]])

    if name == "extras":
        rows = [[ui.button("ok" if extra.id in settings.extras else "no", extra.title, f"st:ext:{extra.id}")]
                for extra in EXTRAS]
        text = (f"{ui.e('blocks')} <b>Дополнения к конспекту</b>\n\n"
                "Основные разделы ИИ выбирает сам по лекции. Здесь можно добавить в конец конспекта то, что нужно не всем. "
                "В режиме «Одно короткое сообщение» дополнения не добавляются.")
        return text, InlineKeyboardMarkup([*rows, [back]])

    if name == "theme":
        presets = [_choice(ui, settings.theme == theme.id, None, theme.title, f"st:set:theme:{theme.id}")
                   for theme in THEMES.values()]
        is_custom = settings.theme == CUSTOM
        custom_label = f"Свой цвет {settings.custom_color}" if is_custom and settings.custom_color else "Свой цвет"
        rows = [presets[i: i + 2] for i in range(0, len(presets), 2)]
        rows.append([_choice(ui, is_custom, "pipette", custom_label, "st:color")])
        text = (f"{ui.e('theme')} <b>Оформление PDF</b>\n\n"
                "Цвета заголовков, акцентов, плашек и таблиц. Свой цвет задаётся HEX-кодом, остальные оттенки бот подберёт сам.")
        return text, InlineKeyboardMarkup([*rows, [back]])

    text = "\n".join(
        [
            f"{ui.e('settings')} <b>Настройки конспектов</b>",
            "",
            f"{ui.e(OUTPUT_ICONS[settings.output])} Формат: <b>{OUTPUT_TITLES[settings.output]}</b>",
            f"{ui.e('length')} Длина: <b>{LENGTH_BY_ID[settings.length].title}</b>",
            f"{ui.e('language')} Язык: <b>{LANGUAGE_TITLES[settings.language]}</b>",
            f"{ui.e('theme')} Оформление PDF: <b>{html(_theme_name(settings))}</b>",
            f"{ui.e('blocks')} Дополнения: <b>{_extras_text(settings)}</b>",
            f"{ui.e('transcript')} Спрашивать про «только текст»: <b>{'да' if settings.ask_raw else 'нет'}</b>",
        ]
    )
    rows = [
        [ui.button(OUTPUT_ICONS[settings.output], "Формат", "st:menu:output"),
         ui.button("length", "Длина", "st:menu:length")],
        [ui.button("language", "Язык", "st:menu:language"), ui.button("theme", "Оформление", "st:menu:theme")],
        [ui.button("blocks", "Дополнения к конспекту", "st:menu:extras")],
        [ui.button("transcript", f"Спрашивать про «только текст»: {'вкл' if settings.ask_raw else 'выкл'}", "st:raw")],
        [ui.button("reset", "Сбросить", "st:reset", style="danger"),
         ui.button("ok", "Готово", "st:close", style="success")],
    ]
    return text, InlineKeyboardMarkup(rows)


def color_prompt(ui: Ui) -> tuple[str, InlineKeyboardMarkup]:
    text = (f"{ui.e('pipette')} Пришлите цвет в формате HEX, например <code>#3B82F6</code>.\n"
            "Цвет должен быть достаточно тёмным, чтобы читаться на белом фоне.")
    return text, InlineKeyboardMarkup([[ui.button("back", "Назад", "st:menu:theme")]])


# --- keys -------------------------------------------------------------------------------


def _rest_note(seconds: float) -> str:
    if seconds <= 0:
        return ""
    if seconds < 120:
        return " · отдыхает ~1 мин"
    if seconds < 7200:
        return f" · отдыхает ~{round(seconds / 60)} мин"
    return f" · отдыхает ~{round(seconds / 3600)} ч"


def keys_view(ui: Ui, keys: list[StoredKey], ring: KeyRing, limit: int, *, prefix: str = "ky",
              title: str = "Ваши ключи Gemini", back: str | None = None,
              statuses: dict[int, str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    statuses = statuses or {}
    lines = [f"{ui.e('key')} <b>{title}</b>", ""]
    if keys:
        for number, key in enumerate(keys, start=1):
            status = statuses.get(key.id, "")
            lines.append(f"{number}. <code>{html(key.masked)}</code>{_rest_note(ring.resting(key.value))}{status}")
        lines += ["", "Бот берёт ключи по очереди и пропускает те, у которых кончился лимит."]
    else:
        lines.append("Ключей пока нет.")
    lines += ["", f'Новый ключ: <a href="{AI_STUDIO}">Google AI Studio</a>. Для запаса нужны ключи из разных проектов '
              "или аккаунтов: у ключей одного проекта общая квота."]

    rows = [[ui.button("trash", f"Удалить {number}. {key.masked}", f"{prefix}:del:{key.id}")]
            for number, key in enumerate(keys, start=1)]
    actions = []
    if len(keys) < limit:
        actions.append(ui.button("add", "Добавить", f"{prefix}:add", style="primary"))
    if keys:
        actions.append(ui.button("search", "Проверить", f"{prefix}:check"))
    if actions:
        rows.append(actions)
    if keys:
        rows.append([ui.button("trash", "Удалить все", f"{prefix}:clear", style="danger")])
    if back:
        rows.append([ui.button("back", "Назад", back)])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def key_prompt(ui: Ui, back: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (f"{ui.e('key')} Пришлите ключ Gemini одним сообщением. Можно несколько, каждый с новой строки.\n\n"
            f'Получить ключ: <a href="{AI_STUDIO}">Google AI Studio</a>')
    return text, InlineKeyboardMarkup([[ui.button("back", "Назад", back)]])


def confirm_clear(ui: Ui, prefix: str) -> tuple[str, InlineKeyboardMarkup]:
    text = f"{ui.e('warning')} Удалить все ключи? Это нельзя отменить."
    return text, InlineKeyboardMarkup([[ui.button("trash", "Да, удалить", f"{prefix}:clear:yes", style="danger"),
                                        ui.button("back", "Отмена", f"{prefix}:show")]])
