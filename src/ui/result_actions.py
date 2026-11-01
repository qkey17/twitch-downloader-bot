"""Stack available result actions; preserve navigation and callback objects."""
from telegram import InlineKeyboardMarkup
from .ui_labels import CHAT_GENERIC, DOWNLOAD_HTML, VOD_LINK


def stack_actions(markup):
    if markup is None:
        return None
    order=(CHAT_GENERIC,DOWNLOAD_HTML,VOD_LINK)
    selected={name:[] for name in order};extra=[]
    for row in markup.inline_keyboard:
        rest=[]
        for button in row:
            if button.text in selected:selected[button.text].append(button)
            else:rest.append(button)
        if rest:extra.append(rest)
    return InlineKeyboardMarkup([[b] for name in order for b in selected[name]]+extra)
