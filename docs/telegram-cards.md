# Telegram cards: identity and lifecycle

This model is implemented in the independent Telegram cards PR. It is not a
production acceptance record.

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

## One lifecycle and one rendering surface

The job card moves through waiting → running → ready, cancelled or error.
A separate render lock serializes progress and terminal delivery, while the
navigation lock serializes user transitions. Terminal phase is set before waiting
for the render lock: an in-flight edit finishes first, then the terminal screen
wins and later progress is ignored.

A safe existing thumbnail is applied before arming the scheduler for first and
regenerated jobs, immediate or queued. Activation cannot be overwritten by a
late waiting/photo edit. No additional Twitch lookup is introduced at each job
transition. Existing URL allowlists, placeholders, full-text fallback and caption
limits remain in force.

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

Opening an already-ready result keeps the existing fresh-final-message behavior
required by the earlier release (no follow-up edit of that final card). Its file
button is signed for that exact generation and is independent of newer input.

## Regression coverage and limits

`test_card_lifecycle.py` groups independent lists/info/results, multiple VODs and
owners, restart, expiry/cache bounds, immediate/queued/regenerated preview,
cancellation after other lists and terminal/progress serialization. Existing
scheduler, cancellation/publication, real isolated registry/history, sender,
terminal error and maintenance tests remain.

Old assertions that a new list must invalidate previous lists, or that every
photo→text transition must send/delete a message, were replaced by independence
and in-place-caption assertions. Exceptional cleanup/replacement tests explicitly
simulate a definite caption refusal instead of relying on normal photo behavior.
Duplicate old-list invalidation/cleanup tests were consolidated into a bounded
independent-list test; they are not silently skipped.

No real Telegram/Twitch calls or production operations are needed for these tests.
Real Telegram delivery and manual UI acceptance remain owner checks. Existing
HTML, schema, storage/lease contracts, infrastructure and PR #27 are unchanged.
