"""Bot-wide state for the admin panel: users, job history, runtime options, access lists."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, fields, replace
from pathlib import Path

ACCESS_MODES = ("open", "whitelist")
DAILY_LIMITS = (0, 3, 5, 10, 20, 50)
DAY = 86_400


@dataclass(frozen=True)
class Options:
    """Settings an admin changes from Telegram. Stored as strings, typed here."""

    model: str
    access: str = "open"
    premium_emoji: bool = True
    daily_limit: int = 0
    shared_keys: bool = False
    maintenance: bool = False
    notify_new_users: bool = False


@dataclass(frozen=True)
class UserRecord:
    user_id: int
    username: str
    full_name: str
    first_seen: float
    last_seen: float
    banned: bool
    notes: int

    @property
    def label(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.full_name or str(self.user_id)


@dataclass(frozen=True)
class Stats:
    users: int
    active_day: int
    active_week: int
    banned: int
    notes_total: int
    notes_day: int
    failures_day: int
    audio_share: float
    average_seconds: float


def _parse(kind: type, raw: str):
    if kind is bool:
        return raw == "1"
    if kind is int:
        return int(raw)
    return raw


def _dump(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class BotState:
    def __init__(self, path: Path, defaults: Options, admin_ids: frozenset[int] = frozenset(),
                 allowed_ids: frozenset[int] = frozenset(), clock=time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._clock = clock
        self._defaults = defaults
        # Admins from the env file always stay admins and cannot be removed from Telegram.
        self.env_admins = admin_ids
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', full_name TEXT NOT NULL DEFAULT '',
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL, banned INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, created_at REAL NOT NULL,
                    kind TEXT NOT NULL, ok INTEGER NOT NULL, seconds REAL NOT NULL, error TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS jobs_user_time ON jobs (user_id, created_at);
                CREATE TABLE IF NOT EXISTS options (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', owner_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS group_topics (
                    chat_id INTEGER NOT NULL, subject TEXT NOT NULL, thread_id INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, subject));
                CREATE TABLE IF NOT EXISTS agreements (user_id INTEGER PRIMARY KEY, version TEXT NOT NULL, accepted_at REAL NOT NULL);
                """
            )
            self._connection.executemany("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", [(i,) for i in allowed_ids])
            if allowed_ids and self._raw_option("access") is None:
                # Version 1 had only the env list: keep the bot closed as before.
                self._connection.execute("INSERT INTO options (name, value) VALUES ('access', 'whitelist')")

    # --- options -------------------------------------------------------------------------

    def _raw_option(self, name: str) -> str | None:
        row = self._connection.execute("SELECT value FROM options WHERE name = ?", (name,)).fetchone()
        return None if row is None else row[0]

    def options(self) -> Options:
        values = {}
        for field in fields(Options):
            raw = self._raw_option(field.name)
            if raw is None:
                continue
            kind = type(getattr(self._defaults, field.name))
            try:
                values[field.name] = _parse(kind, raw)
            except ValueError:
                continue
        options = replace(self._defaults, **values)
        if options.access not in ACCESS_MODES:
            options = replace(options, access="open")
        return options

    def set_option(self, name: str, value) -> Options:
        if name not in {field.name for field in fields(Options)}:
            raise KeyError(name)
        with self._connection:
            self._connection.execute(
                "INSERT INTO options (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, _dump(value)),
            )
        return self.options()

    # --- users ---------------------------------------------------------------------------

    def touch(self, user_id: int, username: str | None, full_name: str | None) -> bool:
        """Record a visit. True if the user is new."""
        now = self._clock()
        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO users (user_id, username, full_name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
                (user_id, username or "", full_name or "", now, now),
            )
            if cursor.rowcount:
                return True
            self._connection.execute(
                "UPDATE users SET username = ?, full_name = ?, last_seen = ? WHERE user_id = ?",
                (username or "", full_name or "", now, user_id),
            )
        return False

    def _record(self, row) -> UserRecord:
        user_id, username, full_name, first_seen, last_seen, banned = row
        notes = self._connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND ok = 1", (user_id,)
        ).fetchone()[0]
        return UserRecord(user_id, username, full_name, first_seen, last_seen, bool(banned), notes)

    def user(self, user_id: int) -> UserRecord | None:
        row = self._connection.execute(
            "SELECT user_id, username, full_name, first_seen, last_seen, banned FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return None if row is None else self._record(row)

    def users(self, offset: int, limit: int) -> list[UserRecord]:
        rows = self._connection.execute(
            "SELECT user_id, username, full_name, first_seen, last_seen, banned FROM users "
            "ORDER BY last_seen DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._record(row) for row in rows]

    def user_count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def recipients(self) -> list[int]:
        return [row[0] for row in self._connection.execute("SELECT user_id FROM users WHERE banned = 0 ORDER BY user_id")]

    def set_banned(self, user_id: int, banned: bool) -> None:
        now = self._clock()
        with self._connection:
            self._connection.execute(
                "INSERT INTO users (user_id, first_seen, last_seen, banned) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET banned = excluded.banned",
                (user_id, now, now, int(banned)),
            )

    def is_banned(self, user_id: int) -> bool:
        row = self._connection.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row[0])

    # --- access lists --------------------------------------------------------------------

    def _ids(self, table: str) -> list[int]:
        return [row[0] for row in self._connection.execute(f"SELECT user_id FROM {table} ORDER BY user_id")]

    def _set_member(self, table: str, user_id: int, member: bool) -> None:
        with self._connection:
            if member:
                self._connection.execute(f"INSERT OR IGNORE INTO {table} (user_id) VALUES (?)", (user_id,))
            else:
                self._connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))

    def whitelist(self) -> list[int]:
        return self._ids("whitelist")

    def set_whitelisted(self, user_id: int, member: bool) -> None:
        self._set_member("whitelist", user_id, member)

    def is_whitelisted(self, user_id: int) -> bool:
        return self._connection.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)).fetchone() is not None

    def admins(self) -> list[int]:
        return sorted(set(self._ids("admins")) | self.env_admins)

    def set_admin(self, user_id: int, member: bool) -> None:
        self._set_member("admins", user_id, member)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.env_admins or (
            self._connection.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone() is not None
        )

    # --- jobs ----------------------------------------------------------------------------

    def group_owner(self, chat_id: int) -> int | None:
        row = self._connection.execute("SELECT owner_id FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    def claim_group(self, chat_id: int, title: str, owner_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title, owner_id) VALUES (?, ?, ?)", (chat_id, title[:100], owner_id)
            )

    def bind_topic(self, chat_id: int, subject: str, thread_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO group_topics (chat_id, subject, thread_id) VALUES (?, ?, ?)", (chat_id, subject, thread_id)
            )

    def unbind_topic(self, chat_id: int, subject: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM group_topics WHERE chat_id = ? AND subject = ?", (chat_id, subject)
            )
        return cursor.rowcount > 0

    def groups_with_topics(self) -> list[tuple[int, str, list[tuple[str, int]]]]:
        """Every group that has topics: (chat id, title, [(subject, topic id)])."""
        rows = self._connection.execute(
            "SELECT g.chat_id, g.title, t.subject, t.thread_id FROM groups g "
            "JOIN group_topics t ON t.chat_id = g.chat_id ORDER BY g.chat_id, t.subject"
        ).fetchall()
        result: dict[int, tuple[str, list]] = {}
        for chat_id, title, subject, thread_id in rows:
            result.setdefault(chat_id, (title, []))[1].append((subject, thread_id))
        return [(chat_id, title, topics) for chat_id, (title, topics) in result.items()]

    def agreed_version(self, user_id: int) -> str | None:
        row = self._connection.execute("SELECT version FROM agreements WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    def accept_agreement(self, user_id: int, version: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO agreements (user_id, version, accepted_at) VALUES (?, ?, ?)",
                (user_id, version, self._clock()),
            )

    def log_job(self, user_id: int, kind: str, ok: bool, seconds: float, error: str = "") -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO jobs (user_id, created_at, kind, ok, seconds, error) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, self._clock(), kind, int(ok), seconds, error[:300]),
            )

    def notes_last_day(self, user_id: int) -> int:
        return self._connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND ok = 1 AND created_at >= ?",
            (user_id, self._clock() - DAY),
        ).fetchone()[0]

    def recent_errors(self, limit: int = 5) -> list[tuple[float, int, str]]:
        return self._connection.execute(
            "SELECT created_at, user_id, error FROM jobs WHERE ok = 0 ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def top_users(self, limit: int = 5) -> list[tuple[UserRecord, int]]:
        rows = self._connection.execute(
            "SELECT user_id, COUNT(*) AS n FROM jobs WHERE ok = 1 GROUP BY user_id ORDER BY n DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for user_id, count in rows:
            record = self.user(user_id)
            if record is not None:
                result.append((record, count))
        return result

    def stats(self) -> Stats:
        now = self._clock()
        one = lambda sql, *args: self._connection.execute(sql, args).fetchone()[0]  # noqa: E731
        notes_total = one("SELECT COUNT(*) FROM jobs WHERE ok = 1")
        audio = one("SELECT COUNT(*) FROM jobs WHERE ok = 1 AND kind = 'audio'")
        return Stats(
            users=one("SELECT COUNT(*) FROM users"),
            active_day=one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", now - DAY),
            active_week=one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", now - 7 * DAY),
            banned=one("SELECT COUNT(*) FROM users WHERE banned = 1"),
            notes_total=notes_total,
            notes_day=one("SELECT COUNT(*) FROM jobs WHERE ok = 1 AND created_at >= ?", now - DAY),
            failures_day=one("SELECT COUNT(*) FROM jobs WHERE ok = 0 AND created_at >= ?", now - DAY),
            audio_share=audio / notes_total if notes_total else 0.0,
            average_seconds=one("SELECT COALESCE(AVG(seconds), 0) FROM jobs WHERE ok = 1"),
        )

    def close(self) -> None:
        self._connection.close()
