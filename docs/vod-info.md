# VOD information and previews

Each VOD row is one full-width `YYYY-MM-DD HH:MM UTC` button opening information.
There is no separate info button or direct selection button in the list.
If the existing result registry confirms ready HTML for that date, it opens the
ready action screen directly instead; see [ready-html-offer.md](ready-html-offer.md).
Dates remain Helix `created_at` (VOD creation, not a guarantee of stream start),
formatted with the existing `_fmt_dt_utc`. Sorting/count/navigation are unchanged.

Info uses already fetched Helix data: channel, title, duration, created_at and
thumbnail_url. Cache limits remain 32 sessions / 500 records / ten API pages /
600-second absolute TTL. Retained titles are capped at 240 characters,
durations 32, thumbnail URLs 1024, dates 64; other unused strings remain removed.
This increases the bounded metadata cache, not image storage; production RAM
impact has not been benchmarked. No images or new records go into SQLite/disk.

Info and ready-action opening may send an image URL to Telegram. The application accepts HTTPS
on the exact `static-cdn.jtvnw.net` authority and `/cf_vods/` image paths, with no
credentials, ports, query, fragment, encoded path or traversal segments.
Accepted image extensions are `.jpg`, `.jpeg`, `.png`. Helix
dimension placeholders are replaced with 480x270. Other/missing URLs fall back
to text. There is no application-side image download or redirect following.
Telegram performs the CDN fetch itself; its handling of CDN redirects cannot
be controlled by this application. No explicit file_id cache is added.

One information record lives on each bounded browser session, valid at most
300 seconds and never beyond the 600-second session TTL. The active screen is
either the list or the chosen VOD, independently of ready HTML. Opening info
increments the revision, renders its full text/actions in the list message,
and then optionally uses the common renderer's `edit_message_media` to add a
photo to that same message. Old date buttons cannot choose a second VOD.
No extra Helix requests, app-side image downloads or persistent records are added.

Text/keyboard delivery precedes optional media. The conservative 1024 UTF-16
caption bound includes markup: longer cards stay complete text with all buttons.
A rejected image keeps the existing text. An ambiguous timeout/cancellation is
conservatively treated as photo for subsequent transitions.

Back restores the original page from the session without fetching Twitch again.
Text-to-text edits in place. Photo-to-text sends a replacement first, transfers
session message ownership under the UI lock, and then deletes only the captured
old ID outside the lock. Deletion failure triggers one bounded attempt to clear
that retired keyboard; callbacks are already inert by revision/nonce/message.
Failed replacement keeps the last working photo/Back. No background task or
periodic cleanup is created. Each Telegram call is bounded to five seconds.

Process first transfers a photo card to text by the same safe replacement,
then calls the existing prepare_chat flow: it rechecks ready HTML or starts the
existing downloader. The runner receives the transferred message ID. Slot,
confirmed cancellation, history/cache and publication semantics remain unchanged.
Confirmed cancellation from a list restores this same inline information card
or the existing ready offer. Direct-link cancellation is unchanged.

Owner, session, revision, nonce, chat/message, expiry and current-operation
checks remain mandatory. Text application and binding are serialized under the
existing session UI lock; captured obsolete cleanup runs outside it. Preview
checks its captured card both before and after waiting for the lock. It cannot
modify another selection or a new channel. Channel replacement best-effort
clears the obsolete information keyboard; old list expiry text is preserved.
Visible inert messages may remain after deletion failure, expiry or restart.

## Preview diagnosis (2026-10-30)

Limited read-only comparison used the existing `gql_post_json` client, the
public client ID from source (no credentials or `.env`), and two archive VODs
per channel. This is public GQL evidence, not captured Helix responses or a
Telegram delivery test. Each returned image URL was checked once; no retries
or alternative sizes were used. Channel contents will change over time.

| Channel | VODs | 480x270 result | Old validator |
| --- | --- | --- | --- |
| asasnow | 2909918861, 2908834571 | `.jpg`, HTTP 200 image/jpeg | Accepted both |
| jeens | 2913949564, 2913073127 | custom `.jpeg`, HTTP 200 image/jpeg | Rejected both |
| the_happy_hob | 2913973367 | vod-secure `/_404/404_processing_480x270.png`, HTTP 403 | Rejected |
| the_happy_hob | 2913030954 | `.jpg`, HTTP 200 image/jpeg | Accepted |
| goremateri | 2913675263, 2911914309 | `.jpg`, HTTP 200 image/jpeg | Accepted both |

The confirmed custom `.jpeg` rejection is fixed on the same exact trusted
CDN/path allowlist. The processing placeholder has its own diagnostic category
and is never sent as a VOD preview. The other reported the_happy_hob failures
remain unexplained; matching Helix URLs and real Telegram delivery were not
available. No evidence justifies size retries, proxy downloads or a file_id cache.
The HTML renderer/meta enrichment are unchanged; that separate path already
uses Helix thumbnail URLs and has different size lookup behavior.

Current preview diagnostics contain fixed categories only, without URLs,
exception contents, users or tokens. The shared renderer reports text fallback,
media edit/delivery failures and cleanup failures. Invalid/missing/placeholder
URLs retain their existing categories. Logger failures cannot break delivery.

References: [Helix videos](https://dev.twitch.tv/docs/api/videos) and
[Telegram Bot API](https://core.telegram.org/bots/api#editmessagemedia).
Only synthetic local tests were run; real Telegram/CDN smoke testing remains
separate. No production deployment or changes to chat/HTML/telemetry are made.
