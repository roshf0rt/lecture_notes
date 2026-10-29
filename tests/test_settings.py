from notes_bot.blocks import DEFAULT_LENGTH
from notes_bot.settings import SettingsStore, UserSettings


def test_defaults_match_the_documented_choices():
    settings = UserSettings()
    assert settings.output == "both"
    assert settings.length == DEFAULT_LENGTH == "long"
    assert settings.theme == "notion"


def test_invalid_stored_values_fall_back_to_defaults():
    settings = UserSettings.from_dict(
        {"output": "docx", "length": "huge", "theme": "neon", "blocks": ["takeaways"]}  # old data may still hold blocks
    )
    assert settings.output == "both"
    assert settings.length == DEFAULT_LENGTH
    assert settings.theme == "notion"
    assert not hasattr(settings, "blocks")


def test_custom_theme_without_valid_colour_falls_back():
    assert UserSettings.from_dict({"theme": "custom", "custom_color": "#FFFF00"}).theme == "notion"
    assert UserSettings.from_dict({"theme": "custom"}).theme == "notion"


def test_custom_theme_keeps_a_valid_colour():
    settings = UserSettings.from_dict({"theme": "custom", "custom_color": "3b82f6"})
    assert settings.theme == "custom"
    assert settings.custom_color == "#3b82f6"


def test_store_roundtrip_and_default_for_new_user(tmp_path):
    store = SettingsStore(tmp_path / "nested" / "settings.sqlite3")
    assert store.get(42) == UserSettings()

    saved = UserSettings(output="tg", length="long", theme="custom", custom_color="#1565a8")
    store.save(42, saved)
    store.close()

    reopened = SettingsStore(tmp_path / "nested" / "settings.sqlite3")
    assert reopened.get(42) == saved
    assert reopened.get(7) == UserSettings()
