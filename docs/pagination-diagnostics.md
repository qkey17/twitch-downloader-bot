# Private Twitch pagination evidence

This opt-in mechanism records what a download actually receives. It does not
retry successful pages, issue control queries, deduplicate, merge, reorder,
restore missing comments, or change the user's chat. Two equal page hashes do
not prove Twitch supplied a complete archive: both responses may omit the same
comments. Instrumentation is disabled by default.

## Enable one VOD and window

Set these environment variables for an independently approved run/restart:

```dotenv
TCD_PAGINATION_DIAGNOSTICS=true
TCD_PAGINATION_DIAG_VOD=123456789
TCD_PAGINATION_DIAG_START=20330
TCD_PAGINATION_DIAG_END=20440
# Optional; substitute the service user's numeric Unix UID:
TCD_PAGINATION_DIAG_DIR=/tmp/tdb-pagination-1000
```

The window uses inclusive original `contentOffsetSeconds`, including fractional
values. Only the selected numeric VOD is instrumented. Every received validated
page gets a summary. Detailed entries cover **all entries of each intersecting
page**, including entries before/after the window; a page spanning the window
qualifies even if no entry falls strictly inside it. Unknown offsets cannot
establish an intersection. No assumption of unique second-level timestamps is
made. Invalid configuration disables only diagnostics with a constant warning.

The supported storage platform is Linux/POSIX (`dir_fd`, `O_NOFOLLOW`, `flock`).
On Windows the recorder fails closed; the ordinary downloader still works.
The directory must be a direct child of real `/tmp`, named
`tdb-pagination-<UID>` with an optional suffix; the default is that exact name.
It must not be inside the project, output, public HTML, spool, or database
directory (including resolved configured roots). Use the same directory for
all processes of one installation: bounds and the lease apply to that root.
Do not put `/tmp` into custom backup sources. The existing application backup
sources and HTML publication do not include this dedicated directory. No
backup/publication configuration is changed by this mechanism.

Unset `TCD_PAGINATION_DIAGNOSTICS` or set it to `false` to disable future
diagnostic operations. No diagnostic files, directory scans, hashes or writes
are performed for disabled/nonselected downloads. No UI configuration exists.
An operation already running keeps its initially selected recorder until it
ends, fails, or reaches the byte limit.

## Format and comparison

Files are NDJSON, schema `tcd-pagination-v1`. Each record contains a diagnostic
operation UUID, VOD ID, event type and UTC time. The operation header includes
the GraphQL operation/query hash and a SHA-256 fingerprint of the relevant
installed Python sources (not a guessed Git SHA). There is no Git subprocess.

- `request`: page, one-based attempt, offset/cursor request type, input cursor
  SHA-256 or offset. Its UTC field is captured immediately before the request.
- `http_response` / `error`: status and bounded technical error category/reason.
  Arbitrary exception messages, headers and response bodies are never recorded.
- `page_received`: validated edge count, structural-valid count, `hasNextPage`,
  output cursor SHA-256, boundary IDs/offsets and `ids_sha256`.
- `message_received`: selected-page zero-based position, original UUID ID,
  exact numeric offset, unchanged UTC `createdAt` string with fractional
  seconds, and `content_sha256`. No author or message text is written.
- `detail_end`: evidence that all detailed entries of that page were recorded.
- `row_processed`: selected-page position to one-based emitted sequence mapping,
  or a discarded blank-text entry. Emission is observed at the original yield.
- `page_emitted`: actual emitted/discarded counts and cumulative emission count.
- `spool_checkpoint`: actual cumulative `ChatSpool.messages` **after** a successful
  batch append. No spool schema/payload changes. Join emitted sequence numbers
  to these cumulative checkpoints to see which received IDs were persisted.
- `pagination_stop`, `operation_end`: termination reason/outcome and totals;
  cancellation/error is distinct from successful end of download.

`valid` means the existing GraphQL validation accepted that edge, not that its
text is nonblank. Blank-text discards are measured later. `null` means not
measured/available, never zero. Spool counts are cumulative checkpoints rather
than invented per-page values: batches can cross page boundaries. Cancellation
can leave a partially emitted page without a `page_emitted` event. A received ID
without a corresponding emitted position was not yet yielded; a yielded
sequence beyond the last checkpoint is not proven committed.

`ids_sha256` is SHA-256 of the ASCII encoding of a JSON array containing **every
original `node.id` in edge order**, including repeated values. Serialization:
`ensure_ascii=True`, separators `(',', ':')`, sorted keys for any JSON objects,
no NaN. This is length-unambiguous (`["ab","c"]` differs from `["a","bc"]`).
Encoding streams in bounded chunks; the full ID array is not copied into RAM.
Missing IDs contribute JSON `null`; `ids_present=false` flags that limitation.
ID order changes the digest even if the set, count, boundaries and cursors match.

`content_sha256` uses the same canonical JSON encoding of
`{"commenter": node.get("commenter"), "message": node.get("message")}`.
This preserves distinctions in queried author/message metadata without writing
their values. ID, offset and createdAt are stored separately. Edge cursors are
not message content. Compare hashes only within this documented schema.

Detailed IDs must match the UUID shape, and timestamps a bounded UTC ISO shape;
unexpected strings are written as `null`, with `identity_complete=false` on the
summary. They still affect the all-ID digest. This prevents unexpected response
fields from being copied into the private file. Raw cursors are **never** stored,
even privately; equal hashes identify equal input/output cursors, but hashes
cannot be used to replay a request. Author names, full text, credentials and
HTTP headers are absent from records and diagnostic warning logs.

## Bounds, permissions and failure behavior

There are four fixed slots, each at most **8 MiB**, at most **32 MiB of diagnostic
file content per configured root**. `gate.lock` is a zero-byte lease file. One
diagnostic writer holds a nonblocking OS lease for its lifetime. Concurrent
downloads continue unchanged; competing diagnostics are skipped with a safe
warning. Completed files reserve their whole slot until eligible for cleanup,
even when small. This deliberately conservative reservation avoids a storage
quota subsystem and cross-process quota races.

Directory permissions must exclude group/others (created 0700), files are
created 0600. Files/directories with unsafe ownership, permissions, symlinks,
hardlinks, ambiguous slots or oversized contents cause recording to be skipped.
Operations use the verified directory descriptor and exclusive/no-follow opens.
The lease is retained through completion/rename. Unrelated filenames are ignored
and never deleted.

At 8 MiB the writer reserves space for a final `truncated` event with
`incomplete=true`, closes and finalizes its diagnostic file, then stops recording.
An interrupted detail sequence without `detail_end` is incomplete. A truncation
marker is not a successful end of pagination and does not change the downloader.
The full page ID digest can already be present while detailed IDs are truncated.

Completed `slot-N.ndjson` files expire after 24 hours. Cleanup occurs lazily on
the next enabled selected operation under the same lease, only after verifying
the owned schema/header/terminal marker. There is no background deletion while
diagnostics are disabled or the service is stopped; expired files may remain
until that next operation. Files from active/crashed/failed writes retain
`slot-N.active` and are never automatically deleted. Such residues reserve a
slot; four residues/full slots disable further recording until independently
reviewed maintenance. Downloads are unaffected. There is no truncation or
overwrite of an existing file to reclaim space.

Any recording/storage failure closes descriptors, releases the lease and
disables diagnostics for that operation with a constant warning. A partially
written `.active` file may have an incomplete final line; do not treat it as
complete evidence. Real Twitch/downloader failures still propagate. The end
event is attempted on success, error and cancellation. No new dependency,
service, database table, publication, cloud upload or backup inclusion exists.

This PR only prepares evidence. Continuation-cursor validation and a bounded
offset-repair pilot require separate approval and measurements. Do not infer a
complete Twitch snapshot from stable hashes or a successful operation.
