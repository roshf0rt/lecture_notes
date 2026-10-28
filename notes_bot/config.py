from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}. См. .env.example.")
    return value


def _user_ids(raw: str) -> frozenset[int]:
    try:
        return frozenset(int(item) for item in raw.replace(" ", "").split(",") if item)
    except ValueError as error:
        raise SystemExit("ALLOWED_USER_IDS должен быть списком чисел через запятую.") from error


@dataclass(frozen=True)
class Config:
    telegram_token: str
    anthropic_api_key: str
    openai_api_key: str | None
    notes_model: str
    transcribe_model: str
    data_dir: Path
    allowed_user_ids: frozenset[int]

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            anthropic_api_key=_required("ANTHROPIC_API_KEY"),
            openai_api_key=env.get("OPENAI_API_KEY", "").strip() or None,
            notes_model=env.get("NOTES_MODEL", "claude-sonnet-5-5"),
            transcribe_model=env.get("TRANSCRIBE_MODEL", "whisper-1"),
            data_dir=Path(env.get("DATA_DIR", "data")),
            allowed_user_ids=_user_ids(env.get("ALLOWED_USER_IDS", "")),
        )
