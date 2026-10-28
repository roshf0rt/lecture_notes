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
    gemini_model: str
    data_dir: Path
    allowed_user_ids: frozenset[int]

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            gemini_model=env.get("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            data_dir=Path(env.get("DATA_DIR", "data")),
            allowed_user_ids=_user_ids(env.get("ALLOWED_USER_IDS", "")),
        )
