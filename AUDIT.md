# TDB — Code Audit

> Historical baseline: this document records the audit of the commit identified below, before subsequent fixes. Several findings have since been addressed. It is preserved as project history, not as a current list of unresolved defects; consult the README, current source, and tests for present behavior.

## 1. Audit scope and baseline

- Repository: `https://github.com/qkey17/twitch-downloader-bot`
- Branch: `main`
- Audited baseline: `ba6bf61fedd8dbc794e66c5d51302ccf87c2fd43`

The audit was based on static inspection of source files. It was not a complete runtime, security, or load test. The findings below are observations from that inspection. Before changing a finding, inspect the relevant code and its callers.

## 2. Critical: credentials and configuration

### 2.1 Credentials embedded in source

- `src/config.py` contains hardcoded Twitch credentials, including an access token.
- `src/twitch_api/badges.py` contains a separate hardcoded Twitch client ID and token.
- Other Twitch API modules use configuration from `src/config.py`.
- Twitch authentication is duplicated and not centrally managed.
- The Telegram bot token is read from an environment variable.

The old Twitch token was stated to be revoked and a new token will be created later. Do not assume all credentials in the repository or its history have been revoked.

### 2.2 Git history

Previous inspection found Twitch access-token variable references in multiple historical versions of `src/config.py`. A separate secret-history check is required; historical token values must not be printed.

### 2.3 Configuration structure

`src/config.py` currently mixes secrets, application settings, server-specific settings, filesystem paths, and public URL settings. Refactor toward a clear separation between environment-provided secrets, application settings, and infrastructure settings, while keeping configuration access consistent.

## 3. Critical: old publication infrastructure

`src/html_publisher.py` currently publishes files into `/var/www/html/public_html`, uses public URL configuration, and has the old server IP embedded in configuration. The old rented server has been removed. Remove the dependency on the old server and filesystem paths, but do not choose a new hosting or storage provider without a separate comparison and user approval.

## 4. Application setup and reproducibility

The repository does not currently contain `requirements.txt`, `pyproject.toml`, `.env.example`, a complete README, or deployment documentation. Runtime dependencies must be identified from imports and actual use.

The application uses relative paths for database and output directories, which can create files in unexpected locations when the working directory changes. Output directory creation does not consistently guarantee that all parent directories exist.

Required direction: add a reproducible dependency manifest, safe environment-variable placeholders, setup and launch documentation, predictable application data directories, path resolution independent of the process working directory, and safe directory creation.

## 5. Confirmed code defects

### 5.1 History callback import

`src/history/history_callbacks.py` imports `_send_history_cards` using an import path that does not match the package structure. Use the appropriate package-qualified import consistent with other modules.

### 5.2 Incorrect cache table name

`get_cache_by_id()` queries a table named `cache`, while the actual table is `vod_cache`. Correct the SQL and check callers while preserving the public API unless a change is necessary.

### 5.3 VOD ID fallback parsing

`src/handlers/handlers_vod.py` can extract any 7–12 digit number from an arbitrary message. This can interpret unrelated numbers as Twitch VOD IDs. Restrict parsing to supported legitimate Twitch link formats.

### 5.4 Initial chat-fetch timeout

`src/workers/download_worker.py` waits approximately 10 seconds for the initial chat fetch. A timeout can be interpreted as an empty chat. Distinguish a genuinely empty chat, a timeout, and a network/API failure. Do not silently report an empty chat when the request failed, and do not change user-facing error messages without approval.

## 6. Task lifecycle and cancellation

Background tasks are not consistently retained and managed. Cancellation can be delayed during slow API responses and retry waits. The per-user `vod_busy` flag can remain set if progress-message editing or task creation fails before `_runner` starts; existing cleanup does not cover every earlier failure point.

Required direction: retain and manage task references, release busy state on every failure path, improve cooperative cancellation during network waits, preserve progress messages/buttons/interaction behavior, and handle active tasks safely during graceful shutdown.

## 7. SQLite and history

- SQLite connections are normally closed, but some functions do not consistently use context managers or `try/finally`; exceptions may leave a connection open.
- `src/database/db_history.py` retrieves history rows and then performs an additional `SELECT` for each row, creating an N+1 query pattern. Optimize only if behavior is preserved.
- Some history callback operations use indices from a currently selected list and refetch the first 10 records. This can produce unstable selection behavior when the list changes or pagination is involved. Review stable internal identifiers without changing the visible interface.
- A cache entry may refer to an HTML file that no longer exists. Verify cached output consistently before returning it, and define a consistent relationship between cache entries and published files for future remote storage. Do not delete existing user data during refactoring.

## 8. Twitch API and optional services

`src/twitch_api/badges.py` and `src/twitch_api/emotes.py` contain different badge retrieval approaches and key formats. Badges are used by `worker_html.py`; check the renderer/template before merging or changing implementations. Do not change the badge key format unless compatibility is verified.

Badge requests do not consistently check HTTP status codes or apply request timeouts. Optional badge-service failures should not unnecessarily prevent HTML generation. `src/twitch_api/meta.py` has broad exception handling around metadata and thumbnail retrieval; distinguish optional thumbnail failures from failures that prevent metadata retrieval. `fetch_comments.py` retries selected network/runtime failures with randomized delays; do not remove retry behavior without a reason and tests.

## 9. HTML viewer compatibility

Relevant files are `src/viewer_template.html` and `src/html_renderer.py`. The template is approximately 40 KB and 1,700 lines. It includes interactive chat rendering, batch loading of 800 messages, filtering over the complete loaded array, emote and badge display, snapshot functionality using `html2canvas`, and additional 7TV emote loading in the browser.

Technical observations include DOM/text insertion for chat text and usernames, JSON serialization escaping `</script>`, `html2canvas` loaded from jsDelivr, and a 7TV endpoint fetched by the browser. Compatibility requirements are to preserve visual appearance, message order/content, filters and behavior, batch loading, emote/badge rendering, snapshot behavior, and the template design. This was static inspection, not a complete browser security audit or visual regression test.

## 10. Handler registration and UI consistency

Callback patterns registered in `main.py` were checked against constants in `src/ui/ui_constants.py`; patterns and constants matched during the audit. The history command import path is correct there, unlike the separate callback import issue. Do not change callback identifiers or registration behavior without demonstrated need.

## 11. Performance and hosting constraints

Separate Telegram users can download simultaneously, and there is no global download concurrency cap. A global cap may be necessary on a resource-limited free host, but it changes application behavior and requires approval. Long VODs can consume substantial RAM and CPU.

Future hosting must support a continuously running Telegram long-polling process and persistent database storage or an approved external database. No provider has been selected.

## 12. Priority summary

### High priority

1. Remove hardcoded credentials and centralize authentication.
2. Check Git history for secrets without exposing values.
3. Remove dependency on the former server.
4. Make setup and configuration reproducible.
5. Fix the history callback import.
6. Fix the incorrect cache table name.
7. Correct VOD ID parsing.
8. Correct timeout versus empty-chat handling.
9. Fix task lifecycle, cancellation, and busy-state cleanup.

### Medium priority

1. Improve SQLite connection cleanup.
2. Make cache and published-file consistency reliable.
3. Review history callback identifiers and pagination.
4. Improve optional badge-service fault tolerance.
5. Consolidate Twitch badge retrieval without changing output format.
6. Improve exception classification in metadata retrieval.

### Lower priority

1. Reduce the N+1 query pattern in history retrieval.
2. Consider parallel third-party emote retrieval while preserving merge precedence.

## 13. Important exclusions

The audit does not authorize a visual redesign, new user-facing features, new commands or Telegram interactions, a webhook migration, a new hosting provider selection, a global concurrency limit, destructive database changes, or changes to the intentional “казуала” joke.
