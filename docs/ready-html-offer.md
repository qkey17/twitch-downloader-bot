# Ready HTML action screen

The channel list still has one full UTC date button per VOD, five rows/page.
After validated owner/message/revision selection, that date checks the existing
`db_results.get_ready_result` registry. It does not trust a cache row by itself:
the registry's current-generation, deletion, reference and local file rules
apply for local/public output. No HTTP link probe, metadata or image request.

For a usable result, consume the normal browser `select` and enter the existing
`prepare_chat`/`offer_chat` screen in the original list TEXT message. No temporary
info card or Process press is required. Without a ready result, the existing
info/Process/Back behavior is unchanged. Older direct-select callbacks remain
compatible. The ready-only date transition checks readiness again in the common
preparation path: if the result disappeared while answering Telegram, it shows
the existing unavailable message with Back and never starts a generation.

`offer_chat` now shows its existing heading followed by channel, title, duration,
message/user counts and creation date, using the same summary formatter as the
history/result cards. Other cards retain their existing output. The offer alone
uses `—` for missing/invalid statistics; actual zero remains zero. Channel/title
are HTML escaped and capped at 120/1200 characters to keep parsed text below
Telegram's 4096 limit, including astral characters. Invalid/unbounded duration or
count values show `—`. Missing other fields retain the existing neutral labels.
No preview on this action screen. No preview API lookup, history entry or change
to the result occurs merely from showing the offer.

Open/Regenerate/Back names and actions are unchanged. Back uses the consumed
selection's revision and restores list ownership, including its current page.
A later selection has a different revision and action nonce. Session/message/
owner/current-request checks reject old actions. Offer text application now uses
the existing session UI lock with a current guard, so a delayed Telegram edit
cannot overtake Back or channel replacement. No background task or cache added.

Open still rechecks ready HTML, records history under the existing rules, shows
text and HTML/VOD links first, then optionally upgrades to a final photo through
PR #17's renderer. Photo failure keeps text; no regeneration occurs. Successful
terminal media still retires the list session. Regenerate still uses the same
single download slot and runner; the ready-only flag cannot bypass that limit.
Direct VOD inputs share the enriched offer; history, terminal cards, HTML files,
publication and SQLite formats remain unchanged.

Tests use synthetic Telegram/Twitch and temporary SQLite/files, including local
and public available/missing/invalid output, disappearance during the transition,
old/foreign/repeated actions, Back/other VOD, delayed offer edits, formatting,
zero/unknown counts and terminal preview/regeneration. Real Telegram smoke tests
remain required for text layout, rapid actions and final image delivery.
