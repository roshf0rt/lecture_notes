from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}. См. .env.example.")
    return value


def _user_ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    try:
        return frozenset(int(item) for item in raw.replace(" ", "").split(",") if item)
    except ValueError as error:
        raise SystemExit(f"{name} должен быть списком чисел через запятую.") from error


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_model: str
    data_dir: Path
    allowed_user_ids: frozenset[int]
    admin_ids: frozenset[int] = frozenset()
    max_parallel_jobs: int = 3
    # Address of a local Bot API server (lifts the 20 MB download limit). Empty = Telegram's cloud API.
    telegram_api_url: str = ""
    # Model name used with keys of the resold gateway. It must be one the key's package allows.
    gateway_model: str = "gemini-3.5-flash"

    @property
    def local_mode(self) -> bool:
        return bool(self.telegram_api_url)

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            gemini_model=env.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
            gateway_model=env.get("GATEWAY_MODEL", "gemini-3.5-flash").strip(),
            data_dir=Path(env.get("DATA_DIR", "data")),
            allowed_user_ids=_user_ids("ALLOWED_USER_IDS"),
            admin_ids=_user_ids("ADMIN_IDS"),
            max_parallel_jobs=max(1, int(env.get("MAX_PARALLEL_JOBS", "3") or 3)),
            telegram_api_url=env.get("TELEGRAM_API_URL", "").strip().rstrip("/"),
        )
