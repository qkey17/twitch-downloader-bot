# TDB — Project Overview

## 1. Project purpose

TDB is a Telegram bot for downloading chat messages from public Twitch VODs and publishing them as interactive HTML pages.

The user sends a Twitch VOD link. The bot retrieves chat messages through Twitch APIs, processes chat metadata and emotes, generates an HTML viewer, publishes the result, and sends the user a link.

The project is intended for a new deployment from a clean repository checkout. The former rented server has been removed and is no longer part of the infrastructure.

## 2. Current baseline

- Repository: `https://github.com/qkey17/twitch-downloader-bot`
- Branch: `main`
- Baseline commit: `ba6bf61fedd8dbc794e66c5d51302ccf87c2fd43`
- The documented baseline had a clean working tree, no untracked files, and matched the specified GitHub commit.

## 3. Existing functionality to preserve

- Accept Twitch VOD links through Telegram.
- Retrieve VOD chat messages using Twitch GraphQL/API access.
- Process chat messages and associated metadata.
- Download VOD-related data and generate interactive HTML.
- Support the existing HTML format-selection workflow.
- Cache previously downloaded VOD data.
- Store user download history in SQLite.
- Provide `/history` and its existing history navigation.
- Preserve existing progress messages, cancellation controls, and callback interactions.
- Preserve existing output formats and the current HTML viewer behavior.
- Preserve the intentional joke about “казуала” after receiving a VOD link; it is deliberate and must not be removed or rewritten.

## 4. Architecture

The application is organized under `src/`.

### Entry point and configuration

- `src/main.py` — startup, Telegram setup, shared HTTP session, handler registration, database initialization, polling, and shutdown.
- `src/config.py` — current configuration and credentials.
- `src/log_setup.py` — logging setup.

### Telegram handlers and UI

- `src/handlers/handlers.py`
- `src/handlers/handlers_cmd.py`
- `src/handlers/handlers_history.py`
- `src/handlers/handlers_misc.py`
- `src/handlers/handlers_state.py`
- `src/handlers/handlers_vod.py`
- `src/ui/ui.py`
- `src/ui/ui_constants.py`
- `src/ui/ui_texts.py`

These modules handle incoming messages, commands, callbacks, VOD requests, user state, messages, labels, keyboards, formatting, and history presentation.

### Twitch API integration

- `src/twitch_api/client.py`
- `src/twitch_api/meta.py`
- `src/twitch_api/fetch_comments.py`
- `src/twitch_api/fetch_page.py`
- `src/twitch_api/emotes.py`
- `src/twitch_api/badges.py`

These modules handle Twitch access, VOD metadata, chat retrieval, badges, and emotes.

### Workers

- `src/workers/download_worker.py`
- `src/workers/worker_html.py`
- `src/workers/worker_progress.py`

These modules handle download processing, HTML preparation, and progress updates.

### HTML generation and publication

- `src/html_renderer.py`
- `src/html_publisher.py`
- `src/viewer_template.html`

The template contains the interactive HTML viewer. Its appearance and behavior must be preserved.

### Database

- `src/database/database.py`
- `src/database/db_core.py`
- `src/database/db_cache.py`
- `src/database/db_history.py`

SQLite is used for caching and user history.

### Main technical characteristics

- Python application.
- Telegram bot using asynchronous handlers and long polling.
- Shared `aiohttp.ClientSession`.
- Twitch GraphQL and related Twitch API requests.
- SQLite database.
- HTML output with JavaScript and CSS.
- External emote and badge services.
- HTML viewer loads chat messages in batches of 800.
- HTML filtering operates on the complete loaded message array.
- Snapshot functionality uses `html2canvas` from jsDelivr.
- Additional 7TV emotes are loaded in the browser.

## 5. Deployment considerations

The previous deployment used a server filesystem path and public URL configuration. The former server no longer exists; its IP address and filesystem layout must not be treated as requirements.

Future deployment needs to account for a continuously running Telegram polling process, persistent SQLite or a separately selected database solution, public hosting or object storage for generated HTML, sufficient memory and CPU for long VOD downloads, and configurable retention and cache consistency.

No hosting or storage provider has been selected.

## 6. Refactoring objective

Prepare the existing application for reliable setup and deployment from a clean checkout while preserving all existing functionality and user-facing behavior. The first priorities are reproducibility, credential security, separation from the former server, confirmed defects, and reliable task lifecycle handling.
