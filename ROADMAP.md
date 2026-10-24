# TDB — Refactoring Roadmap

> Historical plan: this roadmap preserves the original refactoring sequence and stage numbering. Later work was organized into separately approved tasks, so these sections are not a live completion checklist. Consult the README, current source, and tests for the implemented state.

## Objective

Prepare TDB for a reliable new deployment from a clean repository checkout while preserving all existing features, Telegram interactions, messages, HTML appearance, and viewer behavior. Do not redesign the product or add features.

Work is divided into independent stages so changes remain reviewable and unnecessary analysis is avoided.

## Stage 0 — Establish the baseline

Before modifying source code:

1. Read `AGENTS.md`, `PROJECT_OVERVIEW.md`, and `AUDIT.md`.
2. Inspect the current repository state.
3. Identify actual runtime dependencies.
4. Document the current startup sequence.
5. Identify existing database tables and relationships.
6. Record Telegram callback identifiers and user-facing messages.
7. Establish a baseline for generated HTML output and viewer behavior.

Prepare a concise baseline report. Make no source-code changes in this stage.

## Stage 1 — Configuration and credentials

Remove hardcoded credentials, centralize Twitch authentication, use environment variables, separate secrets/application/infrastructure settings, add `.env.example` with safe placeholders, update `.gitignore` as necessary, and inspect tracked files and Git history for possible secrets without printing values. Do not create or revoke credentials; the user will create a new Twitch token separately.

Ready when no active credentials are hardcoded, Twitch API modules use one authentication configuration, missing configuration produces a clear diagnostic, and no secret values appear in logs or reports.

## Stage 2 — Reproducible project setup

Identify runtime dependencies; add `requirements.txt` or `pyproject.toml`; add setup and launch instructions; establish predictable data/output directories; resolve paths independently of the process working directory; create required directories safely; and document environment variables.

Ready when a clean checkout can be installed from the documentation, paths are predictable, and SQLite initializes in a clean environment.

## Stage 3 — Confirmed code defects

With minimal changes: correct the history callback import; make `get_cache_by_id()` use `vod_cache`; restrict VOD ID parsing to supported Twitch link formats; distinguish initial chat-fetch timeouts/network errors from genuinely empty chats; preserve public APIs and user-facing messages; keep the intentional “казуала” message unchanged; and add tests for affected behavior.

## Stage 4 — Task lifecycle and cancellation

Retain references to active tasks, reset busy state on every failure path, improve cancellation during network waits/retries, handle active tasks during graceful shutdown, and avoid orphaned tasks. Preserve the existing progress interface, buttons, and cancellation workflow.

Ready when failed tasks do not leave users permanently busy, cancellation works during slow network operations, a new VOD request works after completion/failure, and shutdown leaves no unmanaged tasks.

## Stage 5 — Database and history reliability

Ensure SQLite connections close reliably; review history selection and pagination; use stable internal identifiers where needed; handle missing cached HTML files consistently; and review the N+1 history query pattern, optimizing only when behavior can be preserved. Do not perform destructive migrations. If a schema change is necessary, stop and request approval.

Ready when existing history/cache data remains accessible, history navigation behaves as before, missing output files do not produce invalid cache responses, and connections are cleaned up.

## Stage 6 — Twitch API and optional services

Consolidate Twitch authentication; review badge retrieval; preserve the badge key format expected by the renderer; improve status checks, timeouts, and error handling; ensure optional third-party failures do not unnecessarily prevent HTML generation; review metadata/thumbnail errors; and optimize sequential third-party requests only if source precedence and final output remain unchanged.

Ready when Twitch requests use one credential source, badge/emote rendering remains compatible, optional failures do not unnecessarily abort chat generation, and retry behavior is appropriate and tested.

## Stage 7 — Publication and storage separation

Remove dependency on the old server IP/filesystem paths; separate HTML generation from publication; introduce a publication/storage interface; keep a local development implementation; and prepare for a future remote-storage implementation.

Do not choose or integrate a hosting provider, do not deploy, and do not select storage before separate evaluation and user approval.

Ready when HTML generation works without the old server, publication uses a clear interface, public URL generation is configurable, and local development remains possible.

## Stage 8 — Regression testing

Add practical tests for configuration, credentials, VOD parsing, Twitch metadata, chat retrieval/timeouts, cancellation, busy-state cleanup, SQLite cache/history, history callbacks, HTML generation, badge/emote compatibility, and publication-interface behavior. Mock Telegram/Twitch responses where possible and use temporary SQLite databases.

Test the HTML viewer against the established baseline, comparing appearance and behavior—not only successful generation.

Ready when existing workflows pass regression tests, Telegram UI and HTML output have no unexpected changes, and test results are reported accurately.

## Stage 9 — Documentation and release readiness

Prepare a README with installation/launch instructions, `.env.example`, a dependency manifest, configuration reference, deployment preparation guide, and troubleshooting notes. Prepare a final report covering changed files, passed and unperformed tests, limitations, and requirements for selecting hosting/storage. Do not deploy or publish without approval.

## Global acceptance criteria

The refactored project must:

1. Start from a clean checkout with correctly configured environment variables.
2. Preserve all existing Telegram commands and interactions.
3. Preserve existing messages and buttons.
4. Preserve the intentional “казуала” joke.
5. Preserve VOD download and HTML generation.
6. Preserve cache and history behavior.
7. Preserve HTML appearance and interactive functionality.
8. Handle slow requests, failures, and cancellation reliably.
9. Not depend on the former server.
10. Not contain active secrets in tracked source files.
11. Have reproducible installation instructions.
12. Have tests covering critical behavior.
13. Avoid unapproved features and behavior changes.

## Execution policy

Do not execute the roadmap as one large operation. Work stage by stage. For each stage: explain the scope, inspect only needed files, propose a small plan, make approved changes, run relevant tests, report changed files and results, and stop before decisions requiring user approval. Explain important decisions plainly, use conservative judgment for unambiguous behavior, and ask for approval when a decision could change behavior, data, UI, infrastructure, or scope.
