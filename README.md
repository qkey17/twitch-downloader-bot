# TDB — Twitch Downloader Bot

TDB is a Telegram bot that downloads chat messages from public Twitch VODs, processes their metadata and emotes, generates the existing interactive HTML viewer, and sends the resulting link to the user. SQLite stores the VOD cache and user download history.

## Requirements

- Python 3.12 (the initially checked version for a clean installation)
- Network access to the Telegram, Twitch, and optional emote/badge services
- A writable project data directory

Node.js is not required to run the Python bot. The HTML viewer loads its browser-side `html2canvas` dependency from its existing CDN URL.

## Clean installation

Run these commands from the repository root.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv-clean
.\.venv-clean\Scripts\python.exe -m pip install --upgrade pip
.\.venv-clean\Scripts\python.exe -m pip install -r requirements.txt
```

### POSIX shell

```sh
python3.12 -m venv .venv-clean
. .venv-clean/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The `.venv-clean` name is only an example. Do not reuse an existing environment when validating a clean installation.

## Environment variables

The application reads credentials from the environment. It does not automatically load `.env` files.

Required:

- `TELEGRAM_BOT_TOKEN_TWITCH`
- `TWITCH_CLIENT_ID`
- `TWITCH_ACCESS_TOKEN`

`TWITCH_CLIENT_ID` and `TWITCH_ACCESS_TOKEN` are used for authenticated Helix requests. GraphQL requests use the separate optional `TWITCH_GQL_CLIENT_ID`; when it is unset, the application uses the public Client ID TwitchDownloader uses for comments. Setting `TWITCH_GQL_CLIENT_ID` overrides that default and does not affect Helix. This fallback belongs to a third-party application and may stop working or be restricted by Twitch; configure a Client ID you control if that happens.

Optional:

- `TCD_DB_PATH` — SQLite path. Without it, the database is `vod_history.db` in the project root.
- `TCD_LOG_LEVEL` — logging level; defaults to `INFO`.
- `TCD_HTML_PUBLISH_MODE` — defaults to `local`; local mode saves HTML under `_out` and does not create a public link.
- `TCD_PUBLIC_HTML_DIR`, `TCD_PUBLIC_BASE_URL` — required only when `TCD_HTML_PUBLISH_MODE=public`.
- `TCD_PUBLIC_HTML_URL_PREFIX` — optional public URL path prefix; defaults to `/public_html`.

Use `.env.example` as a list of variable names and configure the real values through the shell, a process manager, or another secret-management mechanism. Never commit real credentials.

## Running

From the repository root:

```powershell
python -m src.main
```

The startup sequence validates required credentials, initializes SQLite, creates the shared HTTP session, registers Telegram handlers, and starts long polling.

## Local data and output

- Default SQLite database: `vod_history.db` in the project root.
- `TCD_DB_PATH` can point to an existing database; existing databases are not moved automatically.
- Generated HTML files: `_out/chat_<uuid>.html` in the project root. The completion card reports this local path; it does not send the file or a public link.
- `src/viewer_template.html` is loaded from the source tree and must remain present.

## HTML publication limitation

The default local mode writes generated pages to `_out` and stores a local artifact reference in the existing cache field. Cache hits and history entries are usable while that file remains present. Local files are not served to Telegram users as links and are not sent as documents.

Public publication remains available as an explicit mode, but requires `TCD_PUBLIC_HTML_DIR` and `TCD_PUBLIC_BASE_URL`; that directory must be writable and served externally. No hosting or storage provider is selected or configured by default.

## Ubuntu 24.04 LTS / Python 3.12

Place a complete checkout at `/opt/twitch-downloader-bot`, readable by the service user. Create a new Linux virtual environment; do not copy a Windows environment. The following installation commands use an isolated virtual environment, not the system Python package directory:

```sh
sudo apt update
sudo apt install -y python3.12 python3.12-venv ca-certificates
sudo python3.12 -m venv /opt/twitch-downloader-bot/.venv
sudo /opt/twitch-downloader-bot/.venv/bin/python -m pip install -r /opt/twitch-downloader-bot/requirements.txt
```

For a new installation, create a dedicated service account and writable data directories:

```sh
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/twitch-downloader-bot --shell /usr/sbin/nologin tdb
sudo install -d -o tdb -g tdb -m 0750 /var/lib/twitch-downloader-bot /opt/twitch-downloader-bot/_out
```

The service needs read/traverse access to the checkout and virtual environment, and write access to `_out` and the database directory. `_out` is currently fixed relative to the checkout and is initialized even in public mode. SQLite also needs permission to create journal files next to the database.

Create the environment file only for the initial installation; retain the existing file during updates:

```sh
sudo install -o root -g root -m 0600 /opt/twitch-downloader-bot/.env.example /etc/twitch-downloader-bot.env
sudoedit /etc/twitch-downloader-bot.env
```

Replace the three required credential placeholders listed above. Set these non-secret settings:

```ini
TCD_DB_PATH=/var/lib/twitch-downloader-bot/vod_history.db
TCD_HTML_PUBLISH_MODE=local
TCD_LOG_LEVEL=INFO
```

Use `NAME=value` assignments without `export` or shell substitutions. Use absolute filesystem paths. Systemd loads this file through `EnvironmentFile`; the Python application does not load it itself. An unset `TWITCH_GQL_CLIENT_ID` uses the documented fallback; do not enable its placeholder value.

Example `/etc/systemd/system/tdb.service`:

```ini
[Unit]
Description=Twitch Downloader Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=tdb
Group=tdb
WorkingDirectory=/opt/twitch-downloader-bot
EnvironmentFile=/etc/twitch-downloader-bot.env
ExecStart=/opt/twitch-downloader-bot/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=5s
KillSignal=SIGTERM
TimeoutStopSec=infinity
UMask=0027
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

PTB waits for active downloads before shutting down and closing the shared HTTP session. `TimeoutStopSec=infinity` preserves that behavior, so stopping can take as long as an active download. A finite stop timeout permits systemd to terminate unfinished work when that timeout expires.

After configuring the credentials and saving the unit:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now tdb.service
sudo journalctl -u tdb.service -f
# Graceful stop:
sudo systemctl stop tdb.service
```

The default local mode creates HTML in `/opt/twitch-downloader-bot/_out` without a public link. For public mode, set `TCD_HTML_PUBLISH_MODE=public`, an absolute `TCD_PUBLIC_HTML_DIR`, and a valid `TCD_PUBLIC_BASE_URL`. An optional `TCD_PUBLIC_HTML_URL_PREFIX` overrides `/public_html`; use a leading slash and a base URL without a trailing slash. Create the public directory writable by `tdb`, and grant the separate HTTP server read/traverse access to that directory and its HTML files. The HTTP server must serve that directory at the configured public URL prefix; the bot only writes local files and does not run an HTTP server or upload them. Keep SQLite and finished HTML persistent; no automatic removal of finished pages is configured.

## Checks without starting the bot

After installation, syntax and import checks can be run without polling:

```powershell
python -m compileall src
python -c "import src.config; import src.twitch_api; print('imports ok')"
```

These checks do not replace tests against Telegram or Twitch, which require configured credentials and external services.
