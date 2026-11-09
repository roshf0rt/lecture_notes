"""The document action must remain reachable through the existing keyboard."""
import pytest

from notes_bot.menus import MENU_ADMIN, MENU_DOCX, main_keyboard, menu_label
from notes_bot.ui import Ui


@pytest.mark.parametrize("premium", [True, False])
@pytest.mark.parametrize("is_admin", [True, False])
def test_document_action_is_recognized_in_regular_and_premium_keyboards(premium, is_admin):
    keyboard = main_keyboard(Ui(premium=premium), is_admin)
    labels = [menu_label(button.text) for row in keyboard.keyboard for button in row]
    assert labels.count(MENU_DOCX) == 1
    assert (MENU_ADMIN in labels) is is_admin


def test_ordinary_text_with_the_document_label_is_not_a_menu_press():
    assert menu_label("Пожалуйста, Улучшить DOCX") is None
