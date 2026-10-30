# Channel VOD selection

The active channel browser loads every Helix archive page, retaining API order
(`type=archive`, `sort=time`) and public VODs only, deduplicated by ID. A count is
shown only after an absent continuation cursor confirms completion. This is a
completed traversal, not an atomic snapshot of Twitch: the channel can change
while pages are being fetched.

The existing safety bounds remain: 500 retained records, 10 API pages, 32 owner
sessions and an absolute 600-second TTL. A whole traversal has a 60-second
timeout in addition to existing HTTP timeouts. Repeated cursors, limits, errors
and rate limiting fail the whole display; partial totals are never shown as
exact. There is no automatic retry loop. A 429 applies the existing shared
cooldown before another channel request. No per-VOD detail requests are made.
The temporary list retains bounded title, duration and thumbnail metadata
for on-demand info cards; see [vod-info.md](vod-info.md). Other unused strings
are discarded. No persistent cache
or schema changes are introduced.

Each UI page contains five full-width date buttons (`YYYY-MM-DD HH:MM UTC`),
one per VOD, opening its temporary info card or, for ready HTML, the shared action screen
([details](ready-html-offer.md)). There is no separate ℹ️ button
or direct select button. Dates are Helix `created_at` (creation
of the VOD), displayed in UTC, not a promise of the stream's exact start time.
The central page number opens a grid of up to 12 numbers, four per row; larger
grids have group arrows. Navigation and selection edit the same bot message.
Session token, revision, owner, chat and message are checked before selection.

Both active direct input and Process from the list's info card use `prepare_chat`, sharing the
existing metadata validation, publication/history and download/cancellation
lifecycle. With no ready HTML, processing starts immediately. Ready HTML offers
«Получить готовый чат» / «Создать чат заново»; list selections additionally offer return to the same
page until processing starts. Actions expire and are bound to user/message and
request generation. No private data is sent to other users' sessions.

`ChannelBrowser`, `build_channel_vod_card`, the legacy default mode of
`prepare_vod_choice`, the format chooser text and functions remain for reference
and compatibility regression tests. Production routing uses `VodListBrowser`
and the HTML-only entry. Old format/regeneration keyboards are rejected; they cannot
restore the retired confirmation screen. Legacy tests including lower-bound
`+` tests exercise only this preserved, inactive implementation; new active UI
tests assert exact counts without `+`.

Old `ui:result:open:<id>` buttons still resolve the current ready HTML and record
history for the requesting user. Missing results report unavailability without
preparing a new download. Both compatible opening and the active Get ready chat
action edit the existing card using the shared result renderer and its local/
public URL restrictions. Repeated active opening is consumed before awaiting;
Telegram's unchanged-message response does not create a duplicate fallback card.

No production deployment or infrastructure changes accompany this PR. Real
Telegram/Twitch end-to-end interaction still requires a separate smoke test.

## Replaced lists

Accepted replacement of an owner session invalidates the previous token as
before. The list adapter now marks the previous message with
`⚠️ Список устарел. Отправьте имя канала повторно.` and removes its keyboard,
but only if it still owns a confirmed list render. It retains the HTML text
that its own renderer successfully sent, never `Message.text` from a callback
snapshot. The record and an asyncio lock live on the existing bounded/expiring
session; there is no global message registry or background notification task.

Navigation renders and replacement use that session's lock. The lock is not
global; other users remain independent. Selection clears the list render before
any metadata/HTML/progress await, so those later states are never overwritten
by replacement. Returning to the list establishes a fresh confirmed render.
Queued callbacks recheck owner, chat/message, session and revision under the
lock. Removed/restarted/expired sessions get exactly one callback answer:
`⚠️ Этот список устарел. Запросите канал повторно.` No warning message is sent.

An invalid/rejected request that leaves the previous session live does not
mark it stale. Once the browser has accepted replacement, its existing rules
invalidate the old token even if Twitch loading subsequently fails. Warning
editing is best effort, has a ten-second bound, and is attempted at most once;
an unavailable/deleted old message does not prevent rendering the new list.
The new list is rendered and its UI lock released before the old-message edit
starts. Slow cleanup therefore does not delay display or callback interaction
with the new list. Cleanup remains awaited by the original request (including
its existing input-route guard), not a detached task. Reacquiring the old
session's lock and rechecking its list ownership preserves the edit fence.
Expiry, restart or interrupted loading can leave an old keyboard visible; the
safe callback answer handles these cases without guessing message contents.
The coordination covers only list-owned messages, not arbitrary external edits
or a universal Telegram synchronization mechanism. Chat processing, task
concurrency, Twitch fetching/cache limits and busy notices remain unchanged.

## Navigation labels and deferred busy notifications

A single UI page has only VOD buttons. Multi-page lists show
`Стр. current/total`; the picker adds `Выберите страницу` and preserves
the existing groups of twelve numbers. The shared Twitch-link label is now
`🔗 Ссылка на VOD`; URLs and history behavior are unchanged.

The requested automatic busy/free line is **not implemented** in this change.
The two active silent rejection sites are `channel_vods_callback` (the
same-user busy check) and `chat_callback` (busy is combined with callback
validity/lookup checks). A global slot refusal in `_start_download` already
responds; maintenance admission and metadata lookup are not chat processing
and must not be mislabeled as such. Help, history and cancellation remain
independent and unchanged.

The safety obstacle is coordinating edits, not detecting task completion.
`finish_download` synchronously releases an operation-owned slot; it is called
both from runner cleanup and the task done callback. There is no central
version/ownership record for Telegram message edits. List handlers, ready HTML
opening, progress updates and final/error/cancel rendering edit independently,
using both Message.edit_text and Bot.edit_message_text. A retained callback
Message is a snapshot, not a fresh read of the server message. Scheduling an
edit from finish_download with that snapshot could restore old text/buttons
over a newer result; checking an operation or session ID before an awaited
edit alone does not serialize that edit with a concurrent writer.

Per the task's explicit stop condition, this PR contains only the independent
navigation/label changes. A follow-up design needs bounded message-version
tracking and serialized edits for every writer of a notified card, plus an
operation-owned completion hook that handles duplicate release, start failure
and immediate replacement by a new operation. Callback validity must precede
busy classification, formatting/entities and keyboards must be preserved, and
deleted/replaced cards must be discarded. These changes need separate review
before adding deferred Telegram edits; no polling or notification queue has
been introduced here. Busy/free lifecycle regressions remain part of that
follow-up, not a claimed result of this PR.
