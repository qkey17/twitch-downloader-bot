# TDB — Instructions for Codex

## Project rules

Before starting a task:

1. Read this file.
2. Read `PROJECT_OVERVIEW.md`.
3. Read the relevant sections of `AUDIT.md` and `ROADMAP.md`.
4. Inspect only the source files necessary for the current task.

## Non-negotiable constraints

- Preserve all existing user-facing features and workflows.
- Preserve the Telegram bot interface: messages, buttons, callback data, labels, and interaction flow.
- Preserve the HTML viewer's visual appearance and behavior.
- Do not redesign the HTML page or remove existing functionality, even if it appears redundant.
- Do not add user-facing features without explicit approval.
- Do not change behavior unless an approved task requires it or it fixes a confirmed defect.
- Preserve existing data and history semantics. Never perform destructive database migrations without explicit approval.
- Do not replace long polling with webhooks unless explicitly approved.
- Do not select a hosting provider, storage provider, or deployment strategy without explicit approval.
- Do not introduce global download-concurrency limits without explicit approval.
- Preserve the intentional joke/message about “казуала” sent after receiving a VOD link; it must remain unchanged.

## Repository and Git

- Treat the local repository as the authoritative working copy.
- The documented baseline is branch `main`, commit `ba6bf61fedd8dbc794e66c5d51302ccf87c2fd43`.
- Do not run `git reset`, force checkout, or destructive Git commands.
- Do not commit, push, or deploy unless explicitly instructed.
- Never overwrite or discard existing user changes.
- Do not access or depend on the former VPS, its IP address, or its filesystem paths.
- The project must eventually run from a clean checkout with newly configured credentials.

## Secrets

- Use environment variables for secrets.
- Do not treat an unused credential as safe merely because the application no longer uses it.
- Report detected secret names and locations without exposing values.
- Do not create, revoke, or rotate credentials on the user's behalf.

## Code changes

- Prefer small, focused changes.
- Avoid unrelated refactoring.
- Preserve public interfaces and function signatures unless a required, approved change makes it necessary.
- Add or update tests for changed behavior.
- Do not claim that a test passed unless it was actually executed.
- If a required service or credential is unavailable, state which test could not be completed.
- Keep dependencies minimal and document new dependencies.

## Communication and checkpoints

For each task, briefly state the intended scope, work only within that scope, report changed files and reasons, report actual test results, and list unresolved issues and risks.

Stop and ask for approval before changing Telegram messages, buttons, callbacks, or interaction flow; changing the HTML viewer's appearance or behavior; adding or removing user-facing features; changing database semantics or performing destructive migrations; choosing hosting, storage, or deployment providers; changing download concurrency; or making an ambiguous behavior change.

When uncertain, preserve current behavior and ask rather than guessing.
