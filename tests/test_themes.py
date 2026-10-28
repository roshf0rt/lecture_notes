import pytest

from notes_bot.themes import ACCENT_ROLES, THEMES, custom_theme, get_theme, parse_hex, tint

ALL_ROLES = {"blue", "purple", "orange", "green", "yellow", "red", "gray", "ink"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("#3B82F6", "#3b82f6"), ("3b82f6", "#3b82f6"), ("#135", "#113355"), ("  #1565a8 ", "#1565a8")],
)
def test_parse_hex_accepts_common_spellings(raw, expected):
    assert parse_hex(raw) == expected


@pytest.mark.parametrize("raw", ["", "blue", "#12345", "#GGGGGG", "#FFFF00", "#FFFFFF", "#abc"])
def test_parse_hex_rejects_malformed_and_too_light(raw):
    assert parse_hex(raw) is None


def test_tint_mixes_towards_white():
    assert tint("#000000", 0.0) == "#000000"
    assert tint("#000000", 1.0) == "#ffffff"
    assert tint("#2f72c4", 0.9) != "#2f72c4"


def test_presets_define_every_role():
    for theme in THEMES.values():
        assert set(theme.colors) == ALL_ROLES, theme.id


def test_custom_theme_recolours_accents_but_keeps_warning_colours():
    theme = custom_theme("#0f766e")
    for role in ACCENT_ROLES:
        assert theme.colors[role] == "#0f766e"
    assert theme.colors["yellow"] == THEMES["notion"].colors["yellow"]
    assert theme.colors["red"] == THEMES["notion"].colors["red"]


def test_get_theme_falls_back_on_unknown_id():
    assert get_theme("nope", None).id == "notion"
    assert get_theme("custom", "#0f766e").colors["blue"] == "#0f766e"
