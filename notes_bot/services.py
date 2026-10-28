from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .config import Config
from .gemini import AUTH, QUOTA, GeminiError
from .keyring import KeyRing, RotatingModel
from .keys import POOL, KeyVault, mask
from .pipeline import Model
from .settings import SettingsStore
from .storage import BotState, Options
from .ui import Ui, html, without_premium

log = logging.getLogger(__name__)

# Keys come from the user, or from the admin's shared pool when that is switched on.
OWN, SHARED = "own", "shared"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass
class Services:
    config: Config
    store: SettingsStore
    keys: KeyVault
    state: BotState
    # Builds a client for one key and one model name.
    make_client: Callable[[str, str], Model]
    ring: KeyRing = field(default_factory=KeyRing)
    locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    slots: asyncio.Semaphore | None = None

    def __post_init__(self) -> None:
        if self.slots is None:
            self.slots = asyncio.Semaphore(self.config.max_parallel_jobs)

    def options(self) -> Options:
        return self.state.options()

    def ui(self) -> Ui:
        return Ui(premium=self.options().premium_emoji)

    def client(self, api_key: str, model: str | None = None) -> Model:
        return self.make_client(api_key, model or self.options().model)

    def model_for(self, user_id: int) -> tuple[RotatingModel | None, str | None]:
        """A model over the user's own keys, or over the shared pool if allowed."""
        options = self.options()
        own = self.keys.values(user_id)
        if own:
            keys, source = own, OWN
        elif options.shared_keys and (pool := self.keys.values(POOL)):
            keys, source = pool, SHARED
        else:
            return None, None
        return RotatingModel(keys, lambda key: self.make_client(key, options.model), self.ring), source

    def is_admin(self, user_id: int) -> bool:
        return self.state.is_admin(user_id)

    def access_problem(self, user_id: int) -> str | None:
        """None if the user may use the bot, otherwise why not: banned, maintenance or closed."""
        if self.is_admin(user_id):
            return None
        if self.state.is_banned(user_id):
            return "banned"
        options = self.options()
        if options.maintenance:
            return "maintenance"
        if options.access == "whitelist" and not self.state.is_whitelisted(user_id):
            return "closed"
        return None


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def _premium_refused(error: BadRequest) -> bool:
    text = str(error).lower()
    return any(word in text for word in ("emoji", "icon", "style", "entit"))


async def reply(message: Message, text: str, keyboard=None) -> Message:
    try:
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard, link_preview_options=NO_PREVIEW
        )
    except BadRequest as error:
        if not _premium_refused(error):
            raise
        log.warning("Telegram refused premium emoji (%s); sending ordinary ones", error)
        text, keyboard = without_premium(text, keyboard)
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard, link_preview_options=NO_PREVIEW
        )


async def edit(target, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    """Edit a message (or the message under a callback query) with HTML text. Never raises on a BadRequest."""
    send = target.edit_message_text if hasattr(target, "edit_message_text") else target.edit_text
    for attempt in range(2):
        try:
            await send(text, parse_mode=ParseMode.HTML, reply_markup=keyboard, link_preview_options=NO_PREVIEW)
            return
        except BadRequest as error:
            # "message is not modified" when a tap changes nothing is fine to ignore.
            if attempt or not _premium_refused(error):
                return
            log.warning("Telegram refused premium emoji (%s); sending ordinary ones", error)
            text, keyboard = without_premium(text, keyboard)


ADDED, DUPLICATE, FULL = "added", "duplicate", "full"


async def add_keys(services: Services, owner: int, keys: list[str]) -> list[tuple[str, str]]:
    """Check each key against the current model and store the good ones. Returns (masked key, outcome)."""
    report: list[tuple[str, str]] = []
    for api_key in dict.fromkeys(keys):
        masked = mask(api_key)
        if api_key in services.keys.values(owner):
            report.append((masked, DUPLICATE))
            continue
        if services.keys.count(owner) >= services.keys.limit(owner):
            report.append((masked, FULL))
            continue
        try:
            await services.client(api_key).check_key()
        except GeminiError as error:
            report.append((masked, str(error)))
            continue
        except Exception:
            log.exception("Gemini key check failed unexpectedly")
            report.append((masked, "не получилось проверить, попробуйте позже"))
            continue
        services.keys.add(owner, api_key)
        report.append((masked, ADDED))
    return report


def key_report(ui: Ui, report: list[tuple[str, str]]) -> str:
    lines = []
    for masked, outcome in report:
        if outcome == ADDED:
            lines.append(f"{ui.e('ok')} <code>{html(masked)}</code> добавлен")
        elif outcome == DUPLICATE:
            lines.append(f"{ui.e('info')} <code>{html(masked)}</code> уже есть")
        elif outcome == FULL:
            lines.append(f"{ui.e('warning')} <code>{html(masked)}</code> не влез: свободных мест нет")
        else:
            lines.append(f"{ui.e('no')} <code>{html(masked)}</code>: {html(outcome)}")
    return "\n".join(lines)


async def check_keys(services: Services, owner: int) -> dict[int, str]:
    """Live status of every stored key, for the keys menu."""
    statuses: dict[int, str] = {}
    for key in services.keys.list(owner):
        try:
            await services.client(key.value).check_key()
        except GeminiError as error:
            if error.kind == QUOTA:
                statuses[key.id] = " · ⚡ лимит"
                services.ring.rest(key.value, 60)
            elif error.kind == AUTH:
                statuses[key.id] = " · ❌ не работает"
            else:
                statuses[key.id] = " · ⚠️ ошибка"
        except Exception:
            log.exception("Gemini key check failed unexpectedly")
            statuses[key.id] = " · ⚠️ ошибка"
        else:
            statuses[key.id] = " · ✅ работает"
    return statuses
