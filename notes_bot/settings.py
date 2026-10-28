from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from .blocks import BLOCKS, DEFAULT_BLOCKS, DEFAULT_LENGTH, LENGTH_BY_ID
from .themes import CUSTOM, DEFAULT_THEME, THEMES, parse_hex

OUTPUTS = ("pdf", "tg")
DEFAULT_OUTPUT = "pdf"
LANGUAGES = ("auto", "ru")


@dataclass(frozen=True)
class UserSettings:
    output: str = DEFAULT_OUTPUT
    length: str = DEFAULT_LENGTH
    theme: str = DEFAULT_THEME
    custom_color: str | None = None
    blocks: tuple[str, ...] = DEFAULT_BLOCKS
    language: str = "auto"
    send_transcript: bool = False

    def toggle_block(self, block_id: str) -> UserSettings:
        enabled = set(self.blocks) ^ {block_id}
        return replace(self, blocks=tuple(block.id for block in BLOCKS if block.id in enabled))

    def to_dict(self) -> dict:
        return {
            "output": self.output,
            "length": self.length,
            "theme": self.theme,
            "custom_color": self.custom_color,
            "blocks": list(self.blocks),
            "language": self.language,
            "send_transcript": self.send_transcript,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> UserSettings:
        custom_color = parse_hex(raw["custom_color"]) if isinstance(raw.get("custom_color"), str) else None
        theme = raw.get("theme", DEFAULT_THEME)
        if theme == CUSTOM and custom_color is None:
            theme = DEFAULT_THEME
        if theme != CUSTOM and theme not in THEMES:
            theme = DEFAULT_THEME

        stored = raw.get("blocks")
        blocks = (
            tuple(block.id for block in BLOCKS if block.id in stored)
            if isinstance(stored, list)
            else DEFAULT_BLOCKS
        )
        output = raw.get("output")
        length = raw.get("length")
        language = raw.get("language")
        return cls(
            output=output if output in OUTPUTS else DEFAULT_OUTPUT,
            length=length if length in LENGTH_BY_ID else DEFAULT_LENGTH,
            theme=theme,
            custom_color=custom_color,
            blocks=blocks,
            language=language if language in LANGUAGES else "auto",
            send_transcript=raw.get("send_transcript") is True,
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
