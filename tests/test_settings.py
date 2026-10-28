from notes_bot.blocks import BLOCKS, DEFAULT_BLOCKS
from notes_bot.settings import SettingsStore, UserSettings


def test_defaults_match_the_documented_choices():
    settings = UserSettings()
    assert settings.output == "pdf"
    assert settings.length == "medium"
    assert settings.theme == "notion"
    assert settings.blocks == DEFAULT_BLOCKS


def test_toggle_block_switches_off_and_back_on_in_note_order():
    settings = UserSettings().toggle_block("examples")
    assert "examples" in settings.blocks
    assert settings.blocks == tuple(b.id for b in BLOCKS if b.id in settings.blocks)

    restored = settings.toggle_block("examples").toggle_block("concepts").toggle_block("concepts")
    assert restored.blocks == tuple(b.id for b in BLOCKS if b.id in restored.blocks)
    assert "examples" not in restored.blocks


def test_invalid_stored_values_fall_back_to_defaults():
    settings = UserSettings.from_dict(
        {"output": "docx", "length": "huge", "theme": "neon", "blocks": ["takeaways", "bogus"]}
    )
    assert settings.output == "pdf"
    assert settings.length == "medium"
    assert settings.theme == "notion"
    assert settings.blocks == ("takeaways",)


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

    saved = UserSettings(output="tg", length="long", theme="custom", custom_color="#1565a8", blocks=("context",))
    store.save(42, saved)
    store.close()

    reopened = SettingsStore(tmp_path / "nested" / "settings.sqlite3")
    assert reopened.get(42) == saved
    assert reopened.get(7) == UserSettings()
