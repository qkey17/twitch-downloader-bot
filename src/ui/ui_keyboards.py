from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from .ui_constants import *
from .ui_labels import (
    CREATE_CHAT,
    CANCEL
)


def build_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(CREATE_CHAT, callback_data=CB_FMT_HTML_ONLINE),
        ],
        [
            InlineKeyboardButton(CANCEL, callback_data=CB_FORMAT_CANCEL),
        ]
    ])


def build_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([])


def build_about_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([])
