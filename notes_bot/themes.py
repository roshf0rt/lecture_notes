from __future__ import annotations

import re
from dataclasses import dataclass

CUSTOM = "custom"
DEFAULT_THEME = "notion"
ACCENT_ROLES = ("blue", "purple", "orange", "green")
# Relative luminance ceiling for a user colour: keeps headings readable on white.
MAX_LUMINANCE = 0.3
_HEX = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


@dataclass(frozen=True)
class Theme:
    id: str
    title: str
    colors: dict[str, str]


_CLASSIC = {
    "blue": "#2F72C4",
    "purple": "#8E5BBF",
    "orange": "#D9762B",
    "green": "#3C8D5A",
    "yellow": "#C99417",
    "red": "#C94545",
    "gray": "#8A8A86",
    "ink": "#37352F",
}

THEMES: dict[str, Theme] = {
    theme.id: theme
    for theme in (
        Theme("notion", "Классика", _CLASSIC),
        Theme(
            "ocean",
            "Океан",
            {
                "blue": "#1565A8",
                "purple": "#3F5BD0",
                "orange": "#0E8FA3",
                "green": "#0F8A6C",
                "yellow": "#B88A1A",
                "red": "#C24C4C",
                "gray": "#647587",
                "ink": "#16293A",
            },
        ),
        Theme(
            "forest",
            "Лес",
            {
                "blue": "#2F6B4F",
                "purple": "#56794A",
                "orange": "#A0722A",
                "green": "#2E8B57",
                "yellow": "#B39B1B",
                "red": "#A4403E",
                "gray": "#6F735E",
                "ink": "#1D2B22",
            },
        ),
        Theme(
            "sunset",
            "Закат",
            {
                "blue": "#C2410C",
                "purple": "#BE185D",
                "orange": "#EA580C",
                "green": "#15803D",
                "yellow": "#CA8A04",
                "red": "#DC2626",
                "gray": "#8A7F78",
                "ink": "#2A1B14",
            },
        ),
        Theme(
            "lavender",
            "Лаванда",
            {
                "blue": "#5B5FC7",
                "purple": "#7C3AED",
                "orange": "#B45309",
                "green": "#0F766E",
                "yellow": "#A16207",
                "red": "#BE123C",
                "gray": "#6B6880",
                "ink": "#221E33",
            },
        ),
        Theme(
            "graphite",
            "Графит",
            {
                "blue": "#334155",
                "purple": "#475569",
                "orange": "#52525B",
                "green": "#3F3F46",
                "yellow": "#78716C",
                "red": "#7F1D1D",
                "gray": "#71717A",
                "ink": "#18181B",
            },
        ),
    )
}


def _channels(color: str) -> list[int]:
    return [int(color[i : i + 2], 16) for i in (1, 3, 5)]


def _luminance(color: str) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in _channels(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def parse_hex(value: str) -> str | None:
    """Return a normalised #rrggbb colour, or None if it is malformed or too light."""
    match = _HEX.fullmatch(value.strip())
    if match is None:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    color = f"#{digits.lower()}"
    return color if _luminance(color) <= MAX_LUMINANCE else None


def tint(color: str, amount: float = 0.88) -> str:
    """Mix a colour with white; used for soft backgrounds."""
    mixed = [round(channel + (255 - channel) * amount) for channel in _channels(color)]
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def custom_theme(color: str) -> Theme:
    colors = dict(_CLASSIC)
    colors.update({role: color for role in ACCENT_ROLES})
    return Theme(CUSTOM, "Свой цвет", colors)


def get_theme(theme_id: str, custom_color: str | None) -> Theme:
    if theme_id == CUSTOM and custom_color:
        return custom_theme(custom_color)
    return THEMES.get(theme_id, THEMES[DEFAULT_THEME])
