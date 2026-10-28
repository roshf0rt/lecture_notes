"""Premium (custom) emoji for messages and buttons, with plain emoji as the fallback.

IDs come from the "Telegram iOS Icons" pack (https://emoji.wivvi.net). They show up
only when the bot owner has Telegram Premium; an admin can switch them off, and then
the bot uses ordinary emoji everywhere. In messages the tag always carries an ordinary
emoji too, so a client that cannot show the icon shows that emoji instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import KeyboardButtonStyle

# name: (ordinary emoji, custom emoji id)
ICONS: dict[str, tuple[str, str]] = {
    "hello": ("👋", "6041921818896372382"),
    "study": ("🎓", "5938195768832692153"),
    "settings": ("⚙", "6032742198179532882"),
    "key": ("🔒", "6037249452824072506"),
    "help": ("❓", "6030848053177486888"),
    "info": ("ℹ", "6028435952299413210"),
    "admin": ("👑", "5805553606635559688"),
    "back": ("⬅️", "5960671702059848143"),
    "next": ("➡️", "6037622221625626773"),
    "ok": ("✅", "5774022692642492953"),
    "no": ("❌", "5774077015388852135"),
    "trash": ("🗑", "6039522349517115015"),
    "add": ("➕", "5882207227997066107"),
    "pdf": ("📄", "6050643982646513651"),
    "message": ("💬", "6030784887093464891"),
    "format": ("📁", "6037475557082403885"),
    "length": ("🎛", "5771449289972650710"),
    "theme": ("🖌", "6050679691004612757"),
    "pipette": ("👩‍🎨", "5769635757211784031"),
    "blocks": ("🧩", "5837069325034331827"),
    "language": ("🌐", "5776233299424843260"),
    "transcript": ("📝", "5778299625370817409"),
    "reset": ("🔁", "6030657343744644592"),
    "mic": ("🎤", "6030722571412967168"),
    "transcribing": ("🗣", "5769500641835618919"),
    "writing": ("✏️", "6039779802741739617"),
    "rendering": ("🖌", "5811925731785052842"),
    "done": ("🎉", "6041731551845159060"),
    "ai": ("✨", "5940660740758184142"),
    "warning": ("❗️", "6030563507299160824"),
    "quota": ("⚡", "5884428842780594914"),
    "clock": ("⌛️", "5891211339170326418"),
    "stats": ("📊", "5936143551854285132"),
    "users": ("👥", "6032609071373226027"),
    "user": ("👤", "6035084557378654059"),
    "unlocked": ("🔓", "6037496202990194718"),
    "locked": ("🔒", "6037249452824072506"),
    "model": ("🤖", "5983580310292402968"),
    "broadcast": ("📢", "6021418126061605425"),
    "switches": ("🎛", "5776424837786374634"),
    "ban": ("🚫", "5938215362473496448"),
    "shield": ("🛡", "6030445631921721471"),
    "bell": ("🔔", "6039486778597970865"),
    "sleep": ("💤", "5983401171501454028"),
    "speed": ("⏲", "6030537810509828330"),
    "send": ("⬆️", "6039391666547201160"),
    "search": ("🔎", "6032850693348399258"),
    "link": ("🔗", "6028171274939797252"),
    "star": ("⭐️", "6034923938486684992"),
    "chart": ("📈", "5938539885907415367"),
    "calendar": ("📅", "5890937706803894250"),
}

# Note blocks in Telegram messages, keyed by block id.
BLOCK_ICONS: dict[str, tuple[str, str]] = {
    "main_idea": ("🧠", "5864019342873598613"),
    "context": ("🧭", "6030687898141987254"),
    "concepts": ("🎓", "5938195768832692153"),
    "process": ("⚙️", "5850309953293653168"),
    "differences": ("🔃", "5767310088255576068"),
    "examples": ("💡", "5891120964468480450"),
    "warnings": ("❗️", "6030563507299160824"),
    "practice": ("🔨", "5836997023554870252"),
    "takeaways": ("✅", "5774022692642492953"),
    "questions": ("❓", "6030848053177486888"),
}

STYLES = {
    "primary": KeyboardButtonStyle.PRIMARY,
    "success": KeyboardButtonStyle.SUCCESS,
    "danger": KeyboardButtonStyle.DANGER,
}


@dataclass(frozen=True)
class Ui:
    premium: bool = True

    def e(self, name: str) -> str:
        """An icon for an HTML message."""
        emoji, emoji_id = ICONS[name]
        if self.premium:
            return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
        return emoji

    def button(
        self,
        icon: str | None,
        text: str,
        data: str | None = None,
        *,
        url: str | None = None,
        style: str | None = None,
    ) -> InlineKeyboardButton:
        """An inline button. With premium on, the icon goes into icon_custom_emoji_id, not the text."""
        icon_id = None
        if icon is not None:
            emoji, emoji_id = ICONS[icon]
            if self.premium:
                icon_id = emoji_id
            else:
                text = f"{emoji} {text}"
        return InlineKeyboardButton(
            text,
            callback_data=data,
            url=url,
            style=STYLES[style] if style else None,
            icon_custom_emoji_id=icon_id,
        )

    def key(self, icon: str, text: str) -> KeyboardButton:
        """A reply keyboard button. Its text is what the bot receives when it is pressed."""
        emoji, emoji_id = ICONS[icon]
        if self.premium:
            return KeyboardButton(text, icon_custom_emoji_id=emoji_id)
        return KeyboardButton(f"{emoji} {text}")


def html(text: str) -> str:
    return escape(text, quote=False)


def markdown_block_icon(block_id: str, premium: bool, fallback: str) -> str:
    """Block icon for a MarkdownV2 message. The caller escapes the fallback."""
    if not premium or block_id not in BLOCK_ICONS:
        return fallback
    emoji, emoji_id = BLOCK_ICONS[block_id]
    return f"![{emoji}](tg://emoji?id={emoji_id})"


_TAG = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>')
_EMOJI_BY_ID = {emoji_id: emoji for emoji, emoji_id in ICONS.values()}


def without_premium(text: str, keyboard):
    """The same message with ordinary emoji, for when Telegram refuses custom ones."""
    text = _TAG.sub(r"\1", text)
    if isinstance(keyboard, InlineKeyboardMarkup):
        rows = []
        for row in keyboard.inline_keyboard:
            plain_row = []
            for button in row:
                icon = _EMOJI_BY_ID.get(button.icon_custom_emoji_id or "")
                plain_row.append(InlineKeyboardButton(
                    f"{icon} {button.text}" if icon else button.text,
                    callback_data=button.callback_data,
                    url=button.url,
                ))
            rows.append(plain_row)
        keyboard = InlineKeyboardMarkup(rows)
    elif isinstance(keyboard, ReplyKeyboardMarkup):
        rows = [[KeyboardButton(f"{_EMOJI_BY_ID[b.icon_custom_emoji_id]} {b.text}" if b.icon_custom_emoji_id in _EMOJI_BY_ID
                                else b.text) for b in row] for row in keyboard.keyboard]
        keyboard = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                                       input_field_placeholder=keyboard.input_field_placeholder)
    return text, keyboard
