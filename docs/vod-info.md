# VOD information and previews

Each list row contains `dd.mm.yy HH:MM` and `ℹ️`. The list header specifies UTC.
Dates remain Helix `created_at` (VOD creation, not a guarantee of stream start),
converted to UTC for compact display. Sorting/count/navigation are unchanged.

Info uses already fetched Helix data: channel, title, duration, created_at and
thumbnail_url. Cache limits remain 32 sessions / 500 records / ten API pages /
600-second absolute TTL. Retained titles are capped at 240 characters,
durations 32, thumbnail URLs 1024, dates 64; other unused strings remain removed.
This increases the bounded metadata cache, not image storage; production RAM
impact has not been benchmarked. No images or new records go into SQLite/disk.

Only info opening sends an image URL to Telegram. The application accepts HTTPS
on the exact `static-cdn.jtvnw.net` authority and `/cf_vods/` image paths, with no
credentials, ports, query, fragment, encoded path or traversal segments. Helix
dimension placeholders are replaced with 480x270. Other/missing URLs fall back
to text. There is no application-side image download or redirect following.
Telegram performs the CDN fetch itself; its handling of CDN redirects cannot
be controlled by this application. No explicit file_id cache is added.

One info-card record lives on each bounded browser session. Validity is at most
300 seconds and never beyond the source session's expiry. Checks/pruning are
lazy: no background cleanup process/timer. Visible messages can remain after
restart/expiry/deletion failure, but their actions are invalid. Channel
replacement best-effort deletes the old info message; a failed deletion cannot
break the new list. No claim of guaranteed Telegram deletion at a deadline.

Repeated info for the same VOD reuses its card. Changing VOD reuses text via
edit_text or photo via edit_media (also the supported text-to-media transition).
Photo-to-text requires deletion and sending a new text message; if deletion
fails, no additional card is created. Photo send failures fall back to text.
API transactions are bounded (show 10 seconds, close 5 seconds). Rendering uses
the source session's existing UI lock. There is no detached task. A deleted
card can be recreated when Telegram reports the old message is missing.

The original list is not edited by info opening/back. Back deletes only the
info message. Session token, revision, owner, source chat/message, card message,
nonce, state and TTL fence actions. Navigation/replacement invalidate old
actions; concurrent info presses serialize. Callback answer is sent once.

Both the left VOD button and info Process consume selection through the same
browser/UI lock. Before any callback answer or Telegram await, they fence
list_render and invalidate/detach the info card. Both use the same prepare_chat
path on the original TEXT list message: ready HTML offers Open/Regenerate/Back,
and uncached VODs start the existing downloader/progress there. Back from the
offer restores the list and its ownership. No extra preparation message or
photo-to-text edit is needed. The single slot/cancellation rules are unchanged.

The selecting request best-effort deletes its captured info message in finally,
after prepare_chat has rendered the screen or returned/failed. Slow deletion
therefore does not delay that screen, block the session UI lock or prevent a
new channel request. Cleanup can only delete that captured photo/text message;
it cannot edit the list/result or clear a newer info-card record. Failure or
cancellation may leave a visible inert card; its old actions cannot select
again. Cleanup waits at most five seconds and creates no background task.
No history/result preview support is included.

References: [Helix videos](https://dev.twitch.tv/docs/api/videos) and
[Telegram Bot API](https://core.telegram.org/bots/api#editmessagemedia).
Only synthetic local tests were run; real Telegram/CDN smoke testing remains
separate. No production deployment or changes to chat/HTML/telemetry are made.
