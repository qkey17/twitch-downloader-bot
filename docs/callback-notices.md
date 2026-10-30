# Callback notifications

Only the notification/acknowledgement layer changes. Session expiry, owner,
revision, nonce, operation and message checks remain mandatory; no stale action
creates a new session or extends its TTL. No warning messages are sent to chats.

## Diagnosis

Active channel-list callbacks already reported staleness, but used the same
list warning for foreign users. Info actions reused that warning for unrelated
cards. Ready actions and operation-bound Cancel answered empty *before* checking
state, then silently returned. History also acknowledged before checking row or
current-message ownership, silently declined old pages, or sent chat messages
for invalid rows. Its internal dispatcher could answer then delegate to a
second answering handler. Retired menus had a different alert/message.

Malformed history and Cancel variants could miss the narrow patterns in main.
The active chat/info/list routes already match their established prefixes;
they do not need a catch-all. The legacy one-card/format functions remain
internal compatibility adapters; production uses the bounded VodListBrowser
and modern chat flow. Retired format/regeneration callbacks stay retired.

## Policy

| Cause | Notification | Alert |
|---|---|---|
| Expired/replaced list, old revision/message | Кнопка больше неактуальна | No |
| Old card/nonce/action/operation, missing history binding/row | Кнопка больше неактуальна | No |
| Obsolete completed action | Кнопка больше неактуальна | No |
| Valid action blocked by chat processing | Сейчас обрабатывается чат VOD. Повторите попытку позже. | No |
| Valid action/lookup currently in progress | Действие выполняется. Повторите попытку. | No |
| Foreign owner or malformed recognized route | Кнопка больше неактуальна | No |

`ui_labels.py` owns the shared texts. Every recognized stale/invalid button uses
the single `STALE_ACTION_NOTICE`, exactly «Кнопка больше неактуальна» without a
final period, with explicit `show_alert=False`. Compatibility list/completed
names alias that constant; they do not introduce another wording. Successful actions acknowledge without
text. Availability/history-disabled messages retain their meaning. Useful
results/documents and explicit VOD URL replies remain normal action outputs;
warnings alone do not send messages or edit cards.

## Ordering and failures

`CallbackReply` is request-scoped. It records its single attempt *before* the
await, including failure, timeout and cancellation. `answerCallbackQuery` has
a three-second outer bound; TelegramError/timeout is swallowed with a fixed
`callback_answer_failed` diagnostic, without raw errors, callback IDs or URLs.
Cancellation propagates. No cache_time override (Telegram default 0), retry,
background task, global callback cache or client-visible technical reason.

Cheap owner/grammar/state checks precede acknowledgement. List/info/ready/page
callbacks acknowledge before waiting for the UI lock or network delivery, then
revalidate under the existing lock. A request invalidated during that interval
does nothing and never attempts a second answer. List preflight mirrors the
browser's bounded action grammar/ranges; the browser remains authoritative under
the lock. Failed acknowledgement does not start/consume an active action.
Old-card cleanup and preview waits preserve their existing ownership rules.

Cancel checks the same operation again after ACK, immediately before setting
the existing cooperative signal. A newly started operation cannot receive an
older button's signal. PR #20's cleanup/slot-release/restoration flow is unchanged.

History delegation answers only in the selected action handler. Row ownership
remains checked in SQLite. No active history view after restart means a page
button is stale; legacy stable-row actions still require DB ownership. The
latest compatible legacy ready-open has one bounded per-user record to reject
duplicate opens/history writes; it is not a click-history store. It cannot edit
an operation-owned progress message. Legacy missing results notify without
restoring the retired format interface or generating a chat.

Main's fallback is *last in the same PTB handler group*, limited to the existing
histpage/histfiles/histvod/pending_cancel/format_cancel prefixes. PTB uses the
first matching handler in a group, so valid actions reach their normal handlers.
Unknown prefixes remain unhandled: the fallback must not mask new routing bugs.
Tests check both real PTB check_update/ordering and main's registrations.

Notifications cannot be guaranteed if Telegram rejects an expired callback,
the bot was offline, the update was not received/dropped during restart, or the
network fails. A recognized callback permits at most one answer attempt; it is
not a promise of successful delivery. No retry/resend workaround is used.

## Validation

Synthetic tests cover list replacement/TTL/revision, photo message rebinding,
old info nonces, foreign owners, active/busy/pending/completed ready actions,
old/current/completed Cancel, operation replacement during ACK, history
delegation/ownership, delayed locks, timeout/cancellation and strict fallback
scope. Prior PR #16–20 regressions remain mandatory. No production API messages,
generations or deployments are required for these tests.

After a separately approved deployment, manually check notification placement
and duration in Telegram, ordinary toast without a modal/OK, rapid repeated presses,
old photo/Cancel/history buttons, fresh navigation/Back and active actions.
Mocks verify API calls and state protection, not real client display.

Known channel replacement still removes the old list keyboard and appends the
existing list-expiry explanation. An obsolete inline information card has its
keyboard cleared only while it still owns that message. Message replacements
send/bind the new screen first, then delete the captured retired ID outside
the UI lock. If deletion fails, a bounded attempt clears that retired keyboard.
No mass cleanup, background task or arbitrary-prefix fallback is added.
