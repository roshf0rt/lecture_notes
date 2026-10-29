from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# Owner id of the shared keys an admin adds. Telegram user ids are positive.
POOL = 0
MAX_USER_KEYS = 10
MAX_POOL_KEYS = 50
# Google AI Studio keys: the classic "AIza…" and the newer "AQ.…" format.
# Keys of the resold gateway (see gateway.py) start with "sk-cvc-".
GATEWAY_PREFIX = "sk-cvc-"
KEY_PATTERN = re.compile(r"(?:AIza|AQ\.)[0-9A-Za-z_.-]{30,80}|sk-cvc-[0-9a-f]{32,128}")


def is_gateway_key(api_key: str) -> bool:
    """Gateway keys are served by a third party, not by Google; the owner must accept that."""
    return api_key.startswith(GATEWAY_PREFIX)


def _load_or_create_secret(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    secret = Fernet.generate_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(secret)
    return secret


def mask(api_key: str) -> str:
    return f"{api_key[:4]}…{api_key[-4:]}"


@dataclass(frozen=True)
class StoredKey:
    id: int
    value: str

    @property
    def masked(self) -> str:
        return mask(self.value)


class KeyVault:
    """Keeps Gemini keys encrypted, so the database file alone reveals nothing.

    A user can keep several keys; the bot switches between them when one runs out
    of quota. Keys with owner POOL are shared ones added by an admin.
    """

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(_load_or_create_secret(data_dir / "secret.key"))
        self._connection = sqlite3.connect(data_dir / "keys.sqlite3")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS gemini_keys ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, token BLOB NOT NULL, added_at REAL NOT NULL)"
            )
            self._connection.execute("CREATE INDEX IF NOT EXISTS gemini_keys_owner ON gemini_keys (owner)")
            self._migrate_single_keys()

    def _migrate_single_keys(self) -> None:
        """Version 1 kept one key per user in api_keys. Move those rows over once."""
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_keys'"
        ).fetchone()
        if exists is None:
            return
        self._connection.execute(
            "INSERT INTO gemini_keys (owner, token, added_at) SELECT user_id, token, ? FROM api_keys", (time.time(),)
        )
        self._connection.execute("DROP TABLE api_keys")

    def _decrypt(self, token: bytes) -> str | None:
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            return None

    def list(self, owner: int) -> list[StoredKey]:
        rows = self._connection.execute(
            "SELECT id, token FROM gemini_keys WHERE owner = ? ORDER BY id", (owner,)
        ).fetchall()
        keys = [StoredKey(key_id, value) for key_id, token in rows if (value := self._decrypt(token)) is not None]
        return keys

    def values(self, owner: int) -> list[str]:
        return [key.value for key in self.list(owner)]

    def count(self, owner: int) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM gemini_keys WHERE owner = ?", (owner,)).fetchone()[0]

    def owners_with_keys(self) -> int:
        return self._connection.execute(
            "SELECT COUNT(DISTINCT owner) FROM gemini_keys WHERE owner != ?", (POOL,)
        ).fetchone()[0]

    def limit(self, owner: int) -> int:
        return MAX_POOL_KEYS if owner == POOL else MAX_USER_KEYS

    def add(self, owner: int, api_key: str) -> bool:
        """Store the key. False if the owner already has it or has no free slot."""
        if api_key in self.values(owner) or self.count(owner) >= self.limit(owner):
            return False
        with self._connection:
            self._connection.execute(
                "INSERT INTO gemini_keys (owner, token, added_at) VALUES (?, ?, ?)",
                (owner, self._fernet.encrypt(api_key.encode("utf-8")), time.time()),
            )
        return True

    def remove(self, owner: int, key_id: int) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM gemini_keys WHERE owner = ? AND id = ?", (owner, key_id))

    def clear(self, owner: int) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM gemini_keys WHERE owner = ?", (owner,))

    def close(self) -> None:
        self._connection.close()
