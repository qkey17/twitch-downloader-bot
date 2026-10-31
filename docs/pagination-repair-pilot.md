# Experimental one-window pagination repair

This opt-in pilot merges independently observed Twitch Message IDs before chat
rows reach the existing append-only ChatSpool. It repairs one explicitly chosen
main page, including its right boundary second. It is not a completeness claim,
an automatic detector, or a universal repair algorithm. Two routes can omit the
same unknown messages. No live acceptance or deployment is part of this PR.

## Selection

The default is off. Only the exact value `true` enables selection. Example for
the previously investigated page, **not production configuration**:

```dotenv
TCD_PAGINATION_REPAIR_PILOT=true
TCD_PAGINATION_REPAIR_VOD=2913809248
TCD_PAGINATION_REPAIR_START=20343
TCD_PAGINATION_REPAIR_END=20470
TCD_PAGINATION_REPAIR_CONTROL_OFFSET=20330
```

All fields are required for the selected VOD. VOD is 7–12 digits; nonnegative
decimal window endpoints must satisfy `start <= end`. The control offset is an
integer strictly before start; exponent/NaN/infinity/truncation are rejected.
Other VODs use ordinary downloading. Invalid selected configuration fails; an
empty chat or end of stream without verifying the selected window also fails.
Only ordinary start offset zero is supported by the selected pilot.

Choose the control offset before the **whole main page**, not just before the
configured subinterval. It must return an observed second strictly before the
page's first second. That first control second is not presumed complete. Every
control ID earlier than the repair page must already exist with identical data
in the retained prefix. The prefix holds two preceding full main pages. Earlier
or novel IDs outside this bounded evidence cause refusal. This is deliberately
stricter than dropping data that cannot be inserted into committed spool rows.

## Main and control streams

`validated_pages` is the existing page validation/retry/backoff/cursor-cycle
path with two consumers. The ordinary consumer yields unchanged seven-field
tuples. The pilot retains raw nodes with Message ID, exact numeric offset,
original createdAt and source page/position until verification.

The first main page whose end reaches `start` must contain the entire configured
interval. It is held in full, even when start/end select an interior subinterval.
If the interval straddles pages or the page's left second has already been
emitted, the operation fails. No window row is yielded before verification.
Previously emitted prefix rows can already be in the temporary spool; this does
not publish a result. The permanent registry/history are untouched on failure.

The independent control iterator starts by the configured offset and continues
only via its own output cursors. Calls are sequential. Each iterator alone owns
its parent page and original continuation cursor. No control cursor can be
assigned to the main iterator. After checking, consumed main lookahead is emitted
once and that same main iterator resumes after its own last buffered page.

Control walks until an observed second strictly beyond the whole held page's
last second. It must cross both sides of the window; ending on its last second
is insufficient. All boundary-second records stay buffered. Control records
after that last second are a checkable tail, not extra output. The main iterator
must cross the right boundary within two lookahead pages. A same-second group in
the next main page is held together with the original page. Every control-tail
ID must be matched with identical data in held main continuation. Missing IDs
strictly inside observed coverage cause immediate refusal. Missing IDs on the
last partial second or beyond coverage require more lookahead within the same
two-page budget; otherwise the operation fails. No automatic window expansion
or dropping of an unverified tail occurs.

## Identity and order

Union is by Message ID. Different IDs with identical text/author/timestamps stay
distinct. Repeated IDs must have equal exact offsets, original createdAt and a
canonical content fingerprint covering commenter, message and the ordinary
renderer's normalized fragments/text/color/badges. Any conflict fails.

Sorting uses exact offset and UTC timestamp, preserving up to nine fractional
digits of createdAt. Source adjacency forms a partial order. A topological
merge preserves those relationships, including equal timestamps. Unconstrained
ties use first observation order; lexical Message ID order is never used as
time. Conflicting source order fails. The bounded prefix also participates in
these checks but is never emitted again. Main IDs remain present, matched tail
IDs are emitted only from main, and added control IDs are emitted only inside
the held page/boundary group. Existing blank-text filtering still applies;
identity counts and emitted/spooled row counts are distinct.

Later pages are checked for chronological regressions throughout the selected
download. A late violation aborts before publication, even after a successfully
checked window. There is no global all-VOD ID index. Duplicate/conflicting IDs
outside the window that are visible in the bounded recent prefix cause refusal.

## Fixed budgets and memory

| Resource | Ceiling |
|---|---:|
| Windows | 1 |
| Control HTTP attempts, including retry attempts | 4 |
| Main lookahead pages | 2 |
| Control wall time, including retry/pacing/HTTP | 30 s |
| Retained canonical serialized pages/cursors | 4 MiB |
| Retained record occurrences, including repeated control rows | 4096 |
| Conservative retained-object estimate | 32 MiB |
| One incoming successful HTTP response body | 4 MiB |
| Output cursor length | 4096 characters |
| Prefix | 2 main pages |

Budgets are checked before requests and before admitting a decoded page into
retention. Physical attempts use the shared retry loop and consume the four
attempts even on HTTP 429/5xx, network failure or subsequent response validation
failure. Main pacing/retries retain existing limits. There is no parallel query
or unbounded retry loop. The control deadline cancels an in-flight request or
retry sleep. The response reader caps decompressed bytes before JSON decoding
only for selected pilot requests; ordinary HTTP decoding is unchanged.

The serialized cap is **not an RSS guarantee**. A bounded incoming response and
its decoded tree coexist with retained pages during admission. The estimate
uses twice recursively measured node sizes plus 2048 bytes per retained row for
rendering/signatures/merge bookkeeping; it is an additional refusal guard, not
an allocator reservation. JSON parsing, Python/aiohttp overhead and unrelated
downloads still use memory. No whole-VOD accumulation, disk merge, persistent
ID index, new database, file or dependency is added by the pilot. Real workload
RSS and duration remain a separate local acceptance measurement.

## Failure, cancellation and publication

Fail closed: no fallback to an unverified ordinary result. Invalid boundaries,
prefix/tail gaps, conflicts, order problems, limit exhaustion, Twitch failures,
write errors and cancellation propagate through existing worker failure paths.
The worker exhausts comments and successfully flushes spool before HTML build.
Consequently a failed selected operation cannot build or publish a new HTML or
replace the existing ready generation. Spool and generators close via existing
worker finally blocks; pilot-owned buffers release on close. The shared session
is not closed by this consumer. Existing UI, cancellation messages, publication
transactions and data/history semantics are retained.

## Optional evidence

PR #28 diagnostics and repair are independent switches. Without diagnostics
the pilot writes no evidence file. With a journal, `repair_start`,
`repair_request`, `repair_page_received`, `repair_window_verified`,
`repair_row_processed`, and `repair_end` are additive v1 events. Buffered main
and control fetches do not mutate the ordinary active-page emission mapping;
they have branch-qualified receive events instead. `repair_row_processed`
increments global emitted sequence only at actual row handoff, and existing
worker spool checkpoints remain actual committed counts. A verified window is
not a successful operation until the stream and worker finish. Events contain
counts, boundaries and bounded reason codes, not text/authors/raw cursors.
The source fingerprint includes the repair module. Existing private quotas,
permissions and fail-open diagnostic storage behavior still apply. Ordinary
off-mode journal events retain their previous format.

## Offline evidence and next gate

The anonymous committed fixture preserves observed set/offset/order relations
but uses synthetic UUIDs, dates, text and cursors. It checks full A19 `73 -> 106`
(33 additions including one at 05:41:00), separately narrow `123 -> 155` (32),
the original C1 continuation, two IDs lost by a simulated C2 substitution, and
the tail. Full synthetic GraphQL responses test the real row conversion/worker.

Separate local artifacts verify the same code against saved real technical
projections with their existing hashes. Those projections do not contain all
original message text, so they do not reconstruct a real user HTML. The real
IDs/cursors/JSON are not committed. Unit and regression tests use no live Twitch.

After review, a separately authorized local run on one VOD/window may measure
actual ID gain, retained counts/bytes, process RSS, latency, four physical control
attempts, cancellation and preservation of the previous ready result on refusal.
Live acceptance, merge, deployment and production enablement require a separate
owner decision. No production configuration instructions are part of this PR.

`repeated_ids` counts all input ID occurrences minus unique IDs in the full
union, including the retained committed prefix, main records and all control
records, before prefix removal. It is separate from `added_ids`.

`repair_page_received` includes `ids_sha256` (the full ordered ID sequence using
the existing `ids_hash`), `input_cursor_sha256` and `output_cursor_sha256`.
Branch/page/attempt identify each stream independently. Hashing is skipped
without an active private journal, including after recording stops. Equal
hashes identify equal observed sequences, not absolute Twitch completeness.
