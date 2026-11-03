# Telegram cards: identity and lifecycle

PR #52 introduced independent card/job/result ownership. This change completes
its image rendering contract; it is not a production acceptance record.

## Three independent responsibilities

- A navigation card owns its `(owner, chat, message)` identity, request/choice
  nonce and UI lock. `CardContext` scopes only navigation fields, so creating a
  new message cannot replace another message's choice or metadata request.
- A `JobContext` owns one scheduler operation, cancellation flag, progress card
  and publication lifetime. Jobs retain their own card context while running.
  Scheduler admission, two active jobs, 20 waiting jobs, FIFO, same-VOD exclusion
  and publication/cleanup boundaries are unchanged.
- The existing results registry owns generations, ready files and deletion state.
  A Telegram card neither stores file paths nor decides whether a file is usable.

`CardData` retains a single compatibility mirror in the existing user data for
legacy internal readers and commands. Current callbacks read their message's
scoped data, never that mirror as a latest-card authority. A legacy dictionary
can be recovered only when its chat/message binding matches exactly; unbound
last-user choices are not imported into a new card.

## Temporary navigation and memory bounds

The global navigation cache contains at most 128 card contexts. It expires
lazily after 15 minutes without use and evicts the least recently used context
at capacity; it creates no background tasks. An active job retains the context
it needs until cleanup even if the cache evicts it.

The existing browser cache remains limited to 32 sessions, 500 VOD metadata
entries per session and an absolute 10-minute TTL. Sessions are independent by
owner/chat/message. Reopening the same message replaces only its own session.
Information actions share that session's expiry, without a separate five-minute
expiry. Revisions/nonces still consume old actions on transitions of that card.
No old message is edited or stripped of buttons just because another list opens.

After a browser snapshot expires, its opaque index callbacks cannot safely
reconstruct the original list/order: they produce the existing short popup and
leave the message untouched. A ready offer that already knows its VOD retains
its own actions while its card choice is valid, even if the list snapshot expires.
Back needs the original snapshot. These transient navigation actions are not
restored after application restart; there is no persistent queue or new schema.

## Durable ready-file actions

The download button no longer points to `user_data`'s last result or to a job.
Its versioned callback is 64 ASCII bytes: `ui:htmlfile:2` followed by URL-safe
base64 of generation UUID (16 bytes), numeric VOD ID (9 bytes), random nonce
(1 byte) and a 96-bit truncated HMAC-SHA256 tag (12 bytes). The payload supports
the existing normalized VOD ID range, including 20 decimal digits.

The existing bot token signs a domain-separated payload scoped to owner and chat;
no new key/configuration is introduced and no secret or path enters the payload.
Verification also requires the Telegram-supplied source message to be authored
by this bot and to contain this exact attached callback button. This supplies
the message binding without storing every message in RAM or knowing a sent
message's ID before Telegram returns it. Client-provided callback data alone
cannot authorize a different source message. Forwarded/copied user messages and
callbacks absent from the source markup are rejected.

With the same bot token, these new buttons work after restart using the existing
registry and streaming sender. The sender opens **the signed generation**, checks
availability, path, lease, identity, size and hash under its existing contract.
It never substitutes a newer `html_current`. Missing/deleted generations report
unavailability. Send concurrency, admission and shutdown controls remain intact.
Bot token rotation invalidates old signatures. Buttons produced by older releases
with RAM-only random capabilities cannot be reconstructed and show the existing
stale-action popup. New result cards must be opened to obtain durable buttons.

## One input, one image card

A valid channel/VOD input creates one **bot-owned** photo message with the small
neutral repository cover while metadata loads. It never edits the user's input.
Invalid syntax retains the existing short error response. Lists, VOD information,
ready offers, queue/progress, cancellation and final results edit this message.
Opening an already-ready HTML now edits the offer's original message; the former
intentional fresh-final send/delete path is removed. The exact-generation signed
download and its separate streaming document message are unchanged.

The existing Get Users response supplies optional `profile_image_url`. Its original
trusted HTTPS Twitch CDN URL remains in metadata. The approved image-style change
composes that avatar into a 720×405 PNG: centered 240 px circle, central crop without
stretch, LANCZOS interpolation and antialiased mask, original cover background
`#1c1e24`. A small 2x canvas keeps the circle exactly centered on an odd-height raster.
No title, logo, border or decoration is added. Telegram may normalize photo delivery.

Avatar requests reuse the application's aiohttp session, never follow redirects,
cap response time at 3 seconds and bytes at 2 MiB (including unknown content length).
Only PNG/JPEG/WebP/GIF decoders are enabled, animation is rejected, dimensions are
at most 2048 per side / 4 Mpix, and output is capped at 192 KiB. Pillow==12.3.0 is
the single new dependency. Decode/composition runs in a thread, one per application;
cancellation waits for the decoder before releasing its slot. No pending decode
queue is built: concurrent cache misses use the neutral fallback.

An in-memory LRU holds at most 16 results, successful images for 10 minutes and
failures for one minute. The existing bounded browser session keeps its selected
composition until its unchanged expiry. Back restores it; pages do not refetch or
reprocess it. No new files, SQLite tables or permanent media store are introduced.

List → VOD changes media to the existing safe thumbnail. Missing/placeholder
previews use `live-vod.png` for confirmed current LIVE or saved LIVE capture,
otherwise `vod.png`. Both covers are 720×405 PNGs under 10 KB. The LIVE cover adds
a compact muted-red label, while the ordinary VOD cover retains white text.
Only those two repository paths are uploaded, with a 32 KiB cap; compositions use
bounded multipart PNG uploads. Normal Twitch previews remain untouched.

The HTML generator embeds the same two covers as trusted build-time PNG constants
in the existing viewer template, once per document, never from metadata data URIs.
The viewer chooses an allowlisted Twitch thumbnail, otherwise the corresponding
LIVE/VOD constant; an image error assigns the fallback once. LIVE still uses saved
capture metadata, not absence of a picture. Modal link/close/focus behavior, card
geometry and other UI controls remain. Existing HTML files are not rewritten.

Each existing `CardContext` remembers only its observed message surface/source
and one rejected-source/fallback pair. Same-source transitions edit the caption,
so queue/progress/result do not reload an unchanged preview. This observation
expires/evicts with the unchanged navigation cache, not a new state manager.
History and file-card navigation use the same renderer and source message;
history request fences and its lock prevent a late file/metadata response from
overwriting a newer page. Saved LIVE metadata, not a new stream lookup, selects
the history cover.

## Serialized lifecycle and exceptional fallback

The job card moves through waiting → running → ready, cancelled or error.
A separate render lock serializes progress and terminal delivery, while the
navigation lock serializes user transitions. Terminal phase is set before waiting
for the render lock: an in-flight edit finishes first, then the terminal screen
wins and later progress is ignored.

The image, caption and Cancel keyboard are applied together before arming the
scheduler for first and regenerated jobs, immediate or queued. Activation cannot
be overwritten by a late waiting/photo edit. No additional Twitch lookup is
introduced at each job transition. Existing URL allowlists, placeholders,
full-text fallback and caption limits remain in force.

`edit_surface` chooses text or caption for the same message, with one alternate
API call after a definite surface-type rejection. `render_card` is shared by
information, offer, history, cancellation and result paths. Normal photo
transitions retain the photo and edit its caption. A too-long caption or definite
edit refusal can require a replacement; its owner is rebound before bounded old
message cleanup. An ambiguous edit/send timeout never triggers a duplicate send.
No retry loop is introduced. A timeout cannot prove what Telegram displayed.

Accepted cancellation still waits for worker cleanup and publication close before
restoration. Both direct and list jobs use `restore_cancelled_card`: it installs
a fresh offer/create action for the job's own VOD in the same text/photo message.
A surviving browser snapshot adds Back; an evicted/expired snapshot does not
prevent self-contained VOD actions. The old operation token cannot cancel the
next job. Startup failure/error removes Cancel using the same surface mechanism;
one bounded fallback send is allowed only after a definite Telegram rejection.

Telegram may reject an original image or an edit, or the full card may exceed
the conservative 1024 UTF-16-unit caption budget. A bounded repository-cover
attempt precedes text fallback for a definite image refusal. Only a confirmed
replacement changes message identity; binding is updated before old-message
cleanup, and cleanup failure cannot unbind the replacement. Full text is not
truncated. An ambiguous timeout stops that delivery attempt without a competing
send/edit; it cannot establish what Telegram displayed. Existing error handling
releases a failed job's slot and never loops image attempts.

## Regression coverage and limits

`test_card_lifecycle.py` groups independent lists/info/results, multiple VODs and
owners, restart, expiry/cache bounds, immediate/queued/regenerated preview,
cancellation after other lists and terminal/progress serialization. Existing
scheduler, cancellation/publication, real isolated registry/history, sender,
terminal error and maintenance tests remain.

`test_single_message_media.py` uses a stateful synthetic Telegram transport:
one initial send, original avatar → VOD → avatar, pagination, three independent
ready cards and signed downloads after RAM loss, first/regenerated and
immediate/queued jobs, cancellation/restart, covers, ownership, in-flight media,
caption overflow, definite refusal and ambiguous delivery. History regressions
also cover saved LIVE, in-place files, superseded lookups and real isolated
SQLite remaining unchanged.

Old assertions that a new list must invalidate previous lists, or that every
photo→text transition must send/delete a message, were replaced by independence
and in-place-caption assertions. Exceptional cleanup/replacement tests explicitly
simulate a definite caption refusal instead of relying on normal photo behavior.
Duplicate old-list invalidation/cleanup tests were consolidated into a bounded
independent-list test; they are not silently skipped.

Tests asserting the removed two-step optional-preview phase and intentional
fresh-final send/delete sequence are consolidated into the stateful lifecycle
matrix and in-place final/history tests. Their old ordering is not the new
contract. Scheduler, cancellation/publication, registry, sender and maintenance
regressions are retained; no skip decorators substitute for coverage.

No real Telegram/Twitch calls or production operations are needed for these tests.
Real Telegram delivery and manual UI acceptance remain owner checks. Existing
HTML, schema, storage/lease contracts, infrastructure and PR #27 are unchanged.
