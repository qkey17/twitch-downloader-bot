# ui_labels.py
"""
Централизованные текстовые метки для кнопок и коротких лейблов.
Нужно импортировать и использовать взамен "магических" строк.
"""

from typing import Optional

# Кнопки / лейблы
CHAT_HTML_LINK = "🌐 HTML"
CHAT_GENERIC = "📖 Открыть чат"           # общий вариант, если нужно
FILES = "📁 Получить файлы"
VOD_LINK = "🔗 Открыть VOD"
CANCEL = "❌ Отмена"

# Алиасы / переиспользуемые форматы (возможно позже сократить)
OPEN_HTML = CHAT_HTML_LINK

def label_for_fmt(fmt: Optional[str]) -> str:
    if fmt == "html_online":
        return CHAT_HTML_LINK
    return FILES