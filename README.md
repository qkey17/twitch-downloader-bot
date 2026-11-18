# TCD — Twitch Chat Downloader Bot

TCD — Telegram-бот для получения чата Twitch VOD и создания интерактивной HTML-страницы с чатом.

## Возможности

- Получение сообщений чата и метаданных Twitch VOD.
- Создание и публикация интерактивного HTML-просмотрщика.
- Повторное использование готовых результатов, история загрузок и пакетная обработка для Premium.
- Поиск и навигация по сообщениям в HTML-просмотрщике.

## Технологии

Python 3.12, `python-telegram-bot`, `aiohttp`, Twitch GraphQL/Helix, SQLite и встроенный HTML/CSS/JavaScript-просмотрщик. Зависимости закреплены в [`requirements.txt`](requirements.txt).

## Установка и локальный запуск

Клонируйте репозиторий и создайте отдельное виртуальное окружение из корня проекта.

Windows PowerShell:

```powershell
git clone https://github.com/qkey17/twitch-downloader-bot.git
cd twitch-downloader-bot
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux/macOS:

```sh
git clone https://github.com/qkey17/twitch-downloader-bot.git
cd twitch-downloader-bot
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Задайте обязательные переменные окружения и запустите из корня проекта:

```sh
python -m src.main
```

Приложение проверяет настройки, инициализирует SQLite и запускает Telegram long polling.

## Конфигурация и секреты

Обязательные переменные окружения:

- `TELEGRAM_BOT_TOKEN_TWITCH`
- `TWITCH_CLIENT_ID`
- `TWITCH_ACCESS_TOKEN`

Для GraphQL можно задать `TWITCH_GQL_CLIENT_ID`; если он не задан, используется совместимый сторонний Client ID, который Twitch может изменить или ограничить. Helix использует `TWITCH_CLIENT_ID` и `TWITCH_ACCESS_TOKEN`.

Основные необязательные параметры:

- `TCD_ADMIN_USER_ID` — Telegram ID владельца с правами Admin.
- `TCD_HISTORY_ENABLED` — личная история; по умолчанию выключена.
- `TCD_DB_PATH` — путь к SQLite; по умолчанию `vod_history.db` в корне проекта.
- `TCD_HTML_PUBLISH_MODE` — режим `local` по умолчанию или `public`.
- `TCD_PUBLIC_HTML_DIR`, `TCD_PUBLIC_BASE_URL` — нужны для режима `public`.
- `TCD_LOG_LEVEL` — уровень логирования; по умолчанию `INFO`.

Полный список и примеры безопасных имён настроек — в [`.env.example`](.env.example) и [`src/config.py`](src/config.py). Приложение не загружает файл `.env` автоматически: передавайте значения через окружение процесса или менеджер службы. Не помещайте реальные токены и другие секреты в Git, документацию или логи.

## Хранение HTML

В режиме `local` файлы сохраняются в `_out`, публичная ссылка не создаётся. В production используется публичный режим.

Для VOD сохраняется одна актуальная HTML-страница с постоянным случайным именем файла. Успешная повторная обработка атомарно заменяет её содержимое, поэтому ссылка остаётся прежней. SQLite хранит реестр текущего результата и историю. Текущие HTML защищены от автоматической очистки; одноразовая очистка накопленных старых файлов завершена.

## Документация

- [Архитектура проекта](PROJECT_OVERVIEW.md)
- [Актуальная дорожная карта](docs/roadmap.md)
- [Production deployment](docs/oracle-deployment.md)
- [Daily backup](docs/daily-backup.md)
- [Проверка и восстановление backup](docs/backup-restore.md)
- [Offsite backup](docs/offsite-backup.md)
- [Отдельное решение по Local Bot API](docs/local-bot-api.md)

Production TCD работает на Oracle Cloud Always Free. Порядок выпуска, maintenance, защиты данных и восстановления описан в специализированных документах выше.
