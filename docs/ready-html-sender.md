# Internal ready HTML sender (Cloud)

No UI is connected yet. `app.bot_data[html_sender.SENDER_KEY].send(user_id,
chat_id, vod_id, generation_id=None)` returns `SendResult`; a future handler must
first authorize the user, chat and selected result using existing UI ownership.
No paths or URLs are accepted. Omitting generation_id selects current once;
passing an ID selects that exact ready generation, never a replacement.

Files up to **41,943,040 bytes** use `InputFile(..., read_file_handle=False)` and
the existing bot/HTTPX transport. No copy, regeneration or URL/file-URI upload.
Delivery limits do not change generation/storage limits. Connect/pool/write/read
timeouts are 5/2/60/30 seconds; the operation deadline is 300 seconds. Cancellation
joins owned preparation and request unwinding before closing the file and lease;
uninterruptible OS I/O can delay that join beyond the deadline.

One send per user, two per application, no queue or retries, independent of VOD
slots. `uncertain=True` means Telegram may have accepted the upload; never retry
automatically. Errors do not change registry/cache/history or existing links.
Generated files require matching stored size/SHA; legacy without SHA uses its
bounded HTML envelope proof, not proof of historical content authenticity.

## Generation leases

Publication, recovery, cleanup and sender use the same stable per-generation
marker in SPOOL_DIR. **Close releases the OS lock, never unlink the pathname.**
This also applies to orphan and deleted-generation markers: retaining a small
file is safer than splitting live locks between old and new inodes. A marker is
roughly 47 bytes plus filesystem allocation/inode (often a 4 KiB block). Files
accumulate with generations; there is deliberately no automatic marker pruning.
The OS releases locks on process exit, so retained markers are not stale locks.
Existing valid markers are reused; unknown/symlink markers fail closed. Do not
change SPOOL_DIR while owners are active; existing generated lease_root binding
still rejects mismatched locations. Concurrent old/new code during upgrade is
unsupported: stop all old owners before using this protocol.

Sender rechecks the exact row and deletion intent after obtaining the lease,
opens/verifies a regular file without following symlinks, and releases SQLite
before network I/O. Cleanup cannot claim or resume deletion under that lease.
This is cooperative exclusion within TCD, not protection against an external
administrator modifying files while bypassing locks.

## Lifecycle

Tasks are explicitly owned outside PTB's `Application.create_task` set, like
cleanup tasks. TcdApplication.stop first closes sender admission and cancels/joins
uploads, BEFORE the parent PTB stop waits for handlers (which may themselves be
awaiting send()). It then follows ordinary PTB stop semantics for VOD work.
post_stop/post_shutdown repeat sender cleanup idempotently. No upload survives
to PTB HTTP transport shutdown; app.running and the service closing flag deny
admission during shutdown.
Maintenance prepare reports busy while uploads/preparation remain owned and
closes admission atomically before its cleanup drain. No changes to VOD cancel,
Telegram endpoint, SQLite schema, UI, backup tools or pagination flags.
