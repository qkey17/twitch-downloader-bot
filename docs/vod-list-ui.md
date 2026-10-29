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
The temporary list retains ID, channel ID, visibility and `created_at`; unused
titles, URLs, thumbnails and other strings are discarded. No persistent cache
or schema changes are introduced.

Each UI page contains five date buttons. Dates are Helix `created_at` (creation
of the VOD), displayed in UTC, not a promise of the stream's exact start time.
The central page number opens a grid of up to 12 numbers, four per row; larger
grids have group arrows. Navigation and selection edit the same bot message.
Session token, revision, owner, chat and message are checked before selection.

Both active direct input and list selection use `prepare_chat`, sharing the
existing metadata validation, publication/history and download/cancellation
lifecycle. With no ready HTML, processing starts immediately. Ready HTML offers
Open HTML / Regenerate; list selections additionally offer return to the same
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
preparing a new download. Both compatible opening and the active Open HTML
action edit the existing card using the shared result renderer and its local/
public URL restrictions. Repeated active opening is consumed before awaiting;
Telegram's unchanged-message response does not create a duplicate fallback card.

No production deployment or infrastructure changes accompany this PR. Real
Telegram/Twitch end-to-end interaction still requires a separate smoke test.
