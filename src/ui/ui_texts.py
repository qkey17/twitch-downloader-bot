import html
from .ui_formatters import fmt_hhmmss


def build_progress_text(meta: dict, vod_url: str, fmt: str, messages: int, unique_users: int, parts: int, elapsed_s: float, done: bool) -> str:
    title = meta.get("title") or "—"
    channel = meta.get("channel") or "—"
    vod_len = meta.get("vod_len") or "—"
    elapsed = fmt_hhmmss(int(elapsed_s))
    status_line = "Чат загружен. Подготовка HTML…" if done else "Загрузка чата…"

    return (
        f"{status_line}\n\n"
        f"VOD: <code>{vod_url}</code>\n"
        f"Канал: <b>{html.escape(channel)}</b>\n"
        f"Название: <b>{html.escape(title)}</b>\n"
        f"Длительность VOD: <b>{vod_len}</b>\n"
        f"Формат: <b>{fmt.upper()}</b>\n\n"
        f"Сообщений: <b>{messages}</b>\n"
        f"Уникальных пользователей: <b>{unique_users}</b>\n"
        f"Файлов: <b>{parts}</b>\n"
        f"Время обработки: <b>{elapsed}</b>"
    )


def about_text() -> str:
    return (
        "Бот загружает 📖 чат 📹 VOD и показывает список 📹 VODs канала.\n\n"
        "Подробнее — /help."
    )
