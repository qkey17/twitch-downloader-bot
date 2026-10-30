# Progress, cancellation and result labels

## Diagnosis and changes

Before this change `_start_download` edited the source text to a bare
«Загрузка чата…»/Cancel, reserved the single slot, then started `_runner`.
After reservation and its own metadata fetch, `download_and_send` rendered the
full progress card. Both first generation and regeneration shared this path.
The bare edit was an admission/UI check, not a downloader prerequisite.
PR #19's photo regeneration also sent the offer text to a new message before
editing that new message to the bare placeholder: an extra visible transition.

The starter now uses the existing `_build_progress_card` with available
preparation metadata (first generation) or ready-result metadata (regeneration).
It shows zero initial counts, escaped/bounded channel/title, date and duration,
and a working Cancel. Unknown values are neutral. No new API lookup, delayed
placeholder, artificial sleep or background task. A slow worker still has
visible progress and cancellation from admission onwards. The normal direct
input «Проверка VOD…» remains while preparation validates the VOD, before admission.

Text starts edit the same message. Photo regeneration reserves admission first,
then sends the full progress text directly through the PR #17–19 renderer.
The session and runner receive the new real message ID before old-photo cleanup,
which remains bounded and outside the UI lock. Failed admission keeps the offer.
Failed first delivery keeps the original photo/actions; startup failure releases
its reservation through the existing starter, without a second start attempt.

`worker_progress` skips only an identical last *successfully shown* text for the
same operation/message. The worker still refreshes metadata and emits real
progress; changes and failed edits are not suppressed. This removes the duplicate
initial full edit when metadata matches. No Twitch/downloader algorithm changed.

## Confirmed cancellation

Cancel sends a cooperative signal; it does not release admission or restore UI.
The progress callback now includes its operation ID (within Telegram's 64-byte
limit). The existing handler accepts this suffix and validates operation, owner,
chat and message. An older operation's button cannot cancel a new run reusing
the same message. Legacy bare callbacks remain recognized, but cannot act on
a token-bound current UI. No new button/command or persistent format is added.

The runner recognizes the exact existing cancellation exception. Only after
`download_and_send` has unwound (iterator/spool/thread cleanup), failure recording,
publication close and the existing operation-owned `finish_download` succeed
does it attempt restoration. UI code never releases admission itself.
Cleanup errors, unexpected task cancellation and worker errors do not claim
successful user cancellation. If a cancellation signal arrives too late and
the worker completes/commits, the successful result remains. Existing operation
IDs, one-slot admission, cancellation flags and telemetry finalization remain.

## Origin and navigation

One transient `download_ui` record in the user's existing context holds the
operation/owner/VOD/chat/message, request identity, regeneration flag and optional
origin `{token, revision, index}`. No API result/browser copy, SQLite field or
image cache. It is removed on completion, failure and task cancellation, including
cancellation before the runner starts; an old done callback cannot clear a new run.

The bounded list session remains selected during work, with a consumed revision
and no owned list render. Its old callbacks are invalid. TTL (600 s), capacity
and navigation bounds are unchanged; success/failure retires that owned session.

After confirmed cancellation:

- First generation from a live list: restore the original list/page in the
  progress text message, then reuse `show_info` to restore the VOD info card
  with fresh nonce/revision and working Create/Back. This matches the original
  two-message list/info screen; photo failure falls back to text.
- Regeneration from a live list: recheck the current result registry, then reuse
  the ready-only preparation/offer and its text-first optional preview. A fresh
  nonce/revision and current message binding protect Get/Recreate/Back. If the
  old result disappeared, restore the list/info/Create screen instead. Previous
  HTML generations are never removed or altered for UI restoration.
- Direct VOD URL/ID: show exactly «Обработка чата отменена пользователем»,
  no trailing period or invented list navigation.
- Expired/replaced session or a newer input: do not resurrect old callbacks.
  Update only the operation-owned old progress to the same cancellation status;
  preserve the newer list. Exact page restoration is limited to the still-live
  original session. Long operations do not extend its TTL.

Restoration validates owner, original revision, message, operation record and
request before installing actions. Metadata/preview resolution and old-message
cleanup remain outside the UI lock. Fresh binding precedes deletion; cancelled
or delayed deletion cannot remove a newer card. UI restoration is best effort:
Telegram failure/timeout may leave an obsolete visible card, but never an active
old Cancel or prematurely freed slot. Fixed `cancel_restore_failed` diagnostics
reuse the safe preview logger without URLs, exception text or user data.

## Result/history body and edited marker

`build_history_page` used `label_for_fmt('html_online')` both as body information
and, separately, as a URL-button label. PR #19 renamed that label to «Открыть чат»,
which exposed the duplicate. Public result/history bodies now omit only this
redundant line. URL buttons, metadata, statistics, local-file paths and useful
local/missing-local HTML descriptions remain. Other formats are unchanged.

Observed API sequences in synthetic tests:

| Transition | Required delivery |
|---|---|
| Ready photo → final photo | One `editMessageMedia` with final caption and links |
| Ready text → final result | `editMessageText` with links, optional `editMessageMedia` |
| Completed generation → result | Progress text → result text/links, optional photo |
| Initial history | `sendMessage` text/links/navigation, optional media upgrade |
| History photo → next photo | Media edit with the next caption/keyboard |
| Photo → text list | Send text, bind, then best-effort delete the old photo |

These edits carry changed content/controls and are necessary. The duplicated
startup placeholder and exact initial progress repeat were removed; the final
result sequence was preserved. Telegram documents `Message.edit_date` and the
text/media/caption editing methods, but exposes no parameter to hide an edited
badge in those methods. Client rendering has not been tested here; its exact
appearance cannot be inferred from mocked API calls. No resend/delete workaround
is used to hide the marker, and links still precede optional image lookup.

References: [Message](https://core.telegram.org/bots/api#message),
[editMessageText](https://core.telegram.org/bots/api#editmessagetext),
[editMessageMedia](https://core.telegram.org/bots/api#editmessagemedia),
[editMessageCaption](https://core.telegram.org/bots/api#editmessagecaption).

## Validation and manual acceptance

`test_progress_cancel_ui.py` runs the real starter/runner and session protocol
with a gated synthetic worker/publication and Telegram mocks. It covers cleanup
ordering, one slot, exact direct cancellation text, Create/Back/Get after cancel,
previous-result disappearance, expired/newer sessions, repeated/foreign/stale
Cancel, startup failures, timeout/task cancellation, telemetry and API ordering.
The existing temporary-registry cancelled-regeneration test validates that the
previous ready head remains unchanged. Prior list/history/preview/lifecycle and
disk-pipeline regressions remain mandatory.

After a separately authorized deployment, manually check first/regeneration
starts from text and photo, cancellation during metadata/chat/HTML stages, the
restored original page and actions, direct-input cancellation, rapid/new-channel
actions, image fallback, result/history labels and the edited badge on mobile.
Do not claim real Telegram/CDN behavior from these synthetic tests.
