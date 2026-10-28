from __future__ import annotations

from dataclasses import replace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .blocks import BLOCKS, LENGTH_BY_ID, LENGTHS
from .settings import OUTPUTS, UserSettings
from .themes import CUSTOM, THEMES

OUTPUT_TITLES = {"pdf": "PDF-файл", "tg": "Сообщения в Telegram"}
BACK = InlineKeyboardButton("⬅️ Назад", callback_data="st:menu:main")


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _mark(selected: bool, text: str) -> str:
    return f"✅ {text}" if selected else text


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
    return settings


def build_view(name: str, settings: UserSettings) -> tuple[str, InlineKeyboardMarkup]:
    if name == "output":
        rows = [[_button(_mark(settings.output == key, title), f"st:set:output:{key}")] for key, title in OUTPUT_TITLES.items()]
        return "Как присылать конспект:", InlineKeyboardMarkup([*rows, [BACK]])

    if name == "length":
        rows = [[_button(_mark(settings.length == length.id, length.title), f"st:set:length:{length.id}")] for length in LENGTHS]
        text = f"Длина конспекта:\n{LENGTH_BY_ID[settings.length].guidance}"
        return text, InlineKeyboardMarkup([*rows, [BACK]])

    if name == "theme":
        presets = [
            _button(_mark(settings.theme == theme.id, theme.title), f"st:set:theme:{theme.id}")
            for theme in THEMES.values()
        ]
        is_custom = settings.theme == CUSTOM
        custom_label = f"Свой цвет ({settings.custom_color})" if is_custom and settings.custom_color else "Свой цвет"
        rows = [presets[i : i + 2] for i in range(0, len(presets), 2)]
        rows.append([_button(_mark(is_custom, custom_label), "st:color")])
        text = (
            "Оформление PDF: цвета заголовков, акцентов и таблиц.\n"
            "В сообщениях Telegram оформление передаётся эмодзи и жирным шрифтом."
        )
        return text, InlineKeyboardMarkup([*rows, [BACK]])

    if name == "blocks":
        rows = [
            [_button(_mark(block.id in settings.blocks, f"{block.icon} {block.title}"), f"st:blk:{block.id}")]
            for block in BLOCKS
        ]
        return "Блоки конспекта. Нажмите, чтобы включить или выключить:", InlineKeyboardMarkup([*rows, [BACK]])

    text = "\n".join(
        [
            "⚙️ Настройки конспектов",
            "",
            f"Формат: {OUTPUT_TITLES[settings.output]}",
            f"Длина: {LENGTH_BY_ID[settings.length].title}",
            f"Оформление: {_theme_name(settings)}",
            f"Блоков включено: {len(settings.blocks)} из {len(BLOCKS)}",
        ]
    )
    rows = [
        [_button(f"Формат: {OUTPUT_TITLES[settings.output]}", "st:menu:output")],
        [_button(f"Длина: {LENGTH_BY_ID[settings.length].title}", "st:menu:length")],
        [_button(f"Оформление: {_theme_name(settings)}", "st:menu:theme")],
        [_button("Блоки конспекта", "st:menu:blocks")],
        [_button("Готово", "st:close")],
    ]
    return text, InlineKeyboardMarkup(rows)


def color_prompt() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Пришлите цвет в формате HEX, например #3B82F6.\n"
        "Цвет должен быть достаточно тёмным, чтобы читаться на белом фоне."
    )
    return text, InlineKeyboardMarkup([[_button("⬅️ Назад", "st:menu:theme")]])
