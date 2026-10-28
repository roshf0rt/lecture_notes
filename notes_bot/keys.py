from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _load_or_create_secret(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    secret = Fernet.generate_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(secret)
    return secret


class KeyVault:
    """Keeps each user's Gemini key encrypted, so the database file alone reveals nothing."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(_load_or_create_secret(data_dir / "secret.key"))
        self._connection = sqlite3.connect(data_dir / "keys.sqlite3")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS api_keys (user_id INTEGER PRIMARY KEY, token BLOB NOT NULL)"
            )

    def save(self, user_id: int, api_key: str) -> None:
        token = self._fernet.encrypt(api_key.encode("utf-8"))
        with self._connection:
            self._connection.execute(
                "INSERT INTO api_keys (user_id, token) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET token = excluded.token",
                (user_id, token),
            )

    def load(self, user_id: int) -> str | None:
        row = self._connection.execute("SELECT token FROM api_keys WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken:
            return None

    def delete(self, user_id: int) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))

    def close(self) -> None:
        self._connection.close()
