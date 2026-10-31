from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from .blocks import DEFAULT_LENGTH, EXTRA_BY_ID, EXTRAS, LENGTHS
from .themes import CUSTOM, DEFAULT_THEME, THEMES, parse_hex

OUTPUTS = ("both", "both_one", "both_rich", "pdf", "tg", "tg_one")
DEFAULT_OUTPUT = "both"
LANGUAGES = ("auto", "ru")


@dataclass(frozen=True)
class UserSettings:
    output: str = DEFAULT_OUTPUT
    length: str = DEFAULT_LENGTH
    theme: str = DEFAULT_THEME
    custom_color: str | None = None
    language: str = "auto"
    send_transcript: bool = False
    extras: tuple[str, ...] = ()  # closing sections the user asked for, in the order of EXTRAS
    ask_raw: bool = False  # ask before each job whether to make a note or send only the transcript

    def toggle_extra(self, extra_id: str) -> UserSettings:
        chosen = set(self.extras) ^ ({extra_id} & EXTRA_BY_ID.keys())
        return replace(self, extras=tuple(extra.id for extra in EXTRAS if extra.id in chosen))

    def to_dict(self) -> dict:
        return {
            "output": self.output,
            "length": self.length,
            "theme": self.theme,
            "custom_color": self.custom_color,
            "language": self.language,
            "send_transcript": self.send_transcript,
            "extras": list(self.extras),
            "ask_raw": self.ask_raw,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> UserSettings:
        custom_color = parse_hex(raw["custom_color"]) if isinstance(raw.get("custom_color"), str) else None
        theme = raw.get("theme", DEFAULT_THEME)
        if theme == CUSTOM and custom_color is None:
            theme = DEFAULT_THEME
        if theme != CUSTOM and theme not in THEMES:
            theme = DEFAULT_THEME

        output = raw.get("output")
        length = raw.get("length")
        language = raw.get("language")
        return cls(
            output=output if output in OUTPUTS else DEFAULT_OUTPUT,
            length=length if length in {item.id for item in LENGTHS} else DEFAULT_LENGTH,
            theme=theme,
            custom_color=custom_color,
            language=language if language in LANGUAGES else "auto",
            send_transcript=raw.get("send_transcript") is True,
            extras=tuple(extra.id for extra in EXTRAS if extra.id in (raw.get("extras") or [])),
            ask_raw=raw.get("ask_raw") is True,
        )


class SettingsStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
            )

    def get(self, user_id: int) -> UserSettings:
        row = self._connection.execute(
            "SELECT data FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return UserSettings()
        try:
            raw = json.loads(row[0])
        except json.JSONDecodeError:
            raw = {}
        return UserSettings.from_dict(raw if isinstance(raw, dict) else {})

    def save(self, user_id: int, settings: UserSettings) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO user_settings (user_id, data) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
                (user_id, json.dumps(settings.to_dict(), ensure_ascii=False)),
            )

    def close(self) -> None:
        self._connection.close()
