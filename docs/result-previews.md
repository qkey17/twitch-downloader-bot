# Result and history previews

An optional 480x270 preview is added to the existing result/history card. The
HTML/VOD labels, reference validation, history ownership, statistics and page
navigation are unchanged. No new Telegram command or preview button is added.
The VOD list UI from PR #16 is unchanged.

## Sources and limits

Use `thumbnail_preview_url`/`thumbnail_url` already in the current metadata,
including the selected channel-list VOD's bounded in-memory snapshot.
An explicitly empty/missing-image value does not trigger enrichment. Only a
record without either field needs a lookup: one Helix `/videos?id=...` request,
with matching ID validation, a three-second outer timeout, no retries, and no
channel traversal. History fetches metadata only for the displayed record.
There is no new metadata/file_id cache or persistent state. Reopening an old
record may perform another single request. Missing credentials, deleted VODs,
API/rate-limit/network/schema errors all lead to text without changing the row.

URL validation and placeholder recognition reuse PR #16: exact HTTPS
`static-cdn.jtvnw.net/cf_vods/`, `.jpg`/`.jpeg`/`.png`, no credentials, ports,
queries, fragments, encoded traversal or external hosts. Twitch's processing
placeholder is not a VOD image. The application does not download image bytes;
Telegram fetches the CDN URL. No image files or SQLite preview migrations.

## Telegram rendering

`src/ui/result_preview.py` supplies shared optional metadata and card delivery.
Text results/initial history cards show their existing links first, before the
optional lookup. Text → photo and photo → photo use `edit_message_media` with
an `InputMediaPhoto`, HTML caption and the complete existing inline keyboard.
No extra photo message is sent. Required preparation/progress/ready-action
screens stay text until a terminal result is opened.

Captions are limited conservatively to 1024 UTF-16 units INCLUDING HTML markup.
This may decline a preview sooner than Telegram's limit after entity parsing;
it never truncates existing title/statistics/local-path text. Longer cards stay
text and skip optional lookups. A photo error preserves/falls back to text.

When the exact text and keyboard are already shown, callers use the renderer's
`keep_text` contract for the optional image upgrade. A media error returns that
same text message ID: no second edit_text, no new send_message, no deletion.
This applies to standalone results/file cards, newly rendered history text and
the final text result. It does not apply to changing an older photo to a new
record's text. `send_card` also checks the returned ID/current request and binds
any replacement before cleanup, rather than ignoring the renderer result.

Photo → text requires a new text message; the bot does not call edit_text on
a photo. It sends text first, binds the new ID, releases the UI lock, then
best-effort deletes only the old captured message. A failed text send keeps the
old card binding/links. Failed/timed-out deletion can leave a visible obsolete
card; its history callbacks are rejected. Every Telegram request has a five-
second timeout. No cleanup tasks are detached. Cancellation propagates and
leaves any already completed ownership transfer intact.

## Ownership and async ordering

History retains one active view (message ID, owner, selected history ID,
request identity and UI lock) per user's PTB context, plus one latest standalone
file/result card binding for the existing file actions. Starting a new history
view retires both older bindings; a new standalone file card retires its
predecessor. Legacy row actions with no live view still require DB ownership.
Navigation callbacks from an unknown view after restart do nothing; `/history`
opens a new view. URL buttons remain useful as links even on an obsolete card.

Metadata awaits happen outside the UI lock. Requests are admitted and Telegram
edits serialized under that lock; immediately before applying an image the
view/request/message ownership is checked again. Older metadata cannot render
over a newer page, and cleanup cannot clear or edit its replacement. The history
command and history/page callbacks are PTB-owned `block=False` handlers; they
do not change the single download slot or create unmanaged background work.

Ready-result opens retain the existing history-recording rules. Repeated open
presses are consumed; regeneration is blocked while optional delivery is pending.
A successfully displayed photo is a terminal HTML/VOD card. Its old saved
regeneration callback is inert; a source VOD-list session is retired to prevent
an old Back button from editing the photograph as text. The visible ready-action
screen before Open still offers Open/Regenerate/Back. On text fallback its
existing compatible behavior remains. A new input can always select the VOD
again for regeneration. This does not create a new HTML generation on photo error.
If the initial required result text itself had to move to a replacement message,
the source list/actions are retired before deleting the old source, too. The
replacement already contains HTML/VOD URL buttons and needs no callback transfer.

## Diagnostics and verification

Fixed categories reuse the info-card logger: URL missing/rejected/placeholder,
photo edit/send failure, text fallback, plus `metadata_failed`,
`card_delivery_failed` and `card_cleanup_failed`. No URL, ID, exception text,
response body, credential or nonce is logged. Logger failures cannot affect UI.
INFO must be enabled. There are no polling/monitoring tasks.

Synthetic tests cover delivery, old/deleted VODs, limits, actual generation
completion, original source-message processing, ownership and late responses,
all photo/text history transitions, errors/cancellation, and byte-for-byte
preservation of a temporary SQLite database. Real Telegram smoke tests are
still needed: text→photo editing, caption/keyboard layout, image→text replacement,
inaccessible-message cleanup, CDN delivery, local/public HTML links and rapid
history navigation. No Oracle, real-user Telegram messages or load tests run.

References: [Bot API](https://core.telegram.org/bots/api#editmessagemedia),
[PTB 22.7](https://docs.python-telegram-bot.org/en/v22.7/telegram.bot.html),
[Helix Get Videos](https://dev.twitch.tv/docs/api/reference/#get-videos).
