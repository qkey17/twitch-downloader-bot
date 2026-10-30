# ui_labels.py
"""
Централизованные текстовые метки для кнопок и коротких лейблов.
Нужно импортировать и использовать взамен "магических" строк.
"""

from typing import Optional

# Кнопки / лейблы
CHAT_HTML_LINK = "Открыть чат"
CHAT_GENERIC = CHAT_HTML_LINK
READY_CHAT = "Чат VOD уже готов"
GET_READY_CHAT = "Получить готовый чат"
CREATE_CHAT = "Создать чат"
RECREATE_CHAT = "Создать чат заново"
BACK_TO_LIST = "Вернуться к списку"
READY_UNAVAILABLE = "Готовый чат недоступен. Выберите VOD заново."
FILES = "📁 Получить файлы"
VOD_LINK = "🔗 Ссылка на VOD"
CANCEL = "❌ Отмена"

# Алиасы / переиспользуемые форматы (возможно позже сократить)
OPEN_HTML = CHAT_HTML_LINK

def label_for_fmt(fmt: Optional[str]) -> str:
    if fmt == "html_online":
        return CHAT_HTML_LINK
    return FILES
