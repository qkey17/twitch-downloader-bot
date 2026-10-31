# Experimental one-window pagination repair

This opt-in pilot merges independently observed Twitch Message IDs before chat
rows reach the existing append-only ChatSpool. It repairs one selected pair:
the unissued preceding page P and the START-selected target T, including the
right boundary second of T. It is not a completeness claim,
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

Choose the control offset before the **whole pending page P**, not just before
T or the configured subinterval. The first control response must begin in a
second strictly before min(first_second(P), int(start)). That first control
second is not presumed complete. Every control ID before first_second(P) must
already exist with identical data in the committed cache C. C holds the last
two completely released main pages; it does not include P. Earlier
or novel IDs outside this bounded evidence cause refusal. This is deliberately
stricter than dropping data that cannot be inserted into committed spool rows.

## Main and control streams

`validated_pages` is the existing page validation/retry/backoff/cursor-cycle
path with two consumers. The ordinary consumer yields unchanged seven-field
tuples. The pilot retains raw nodes with Message ID, exact numeric offset,
original createdAt and source page/position until verification.

The first main page whose end reaches `start` is selected as T. Its first offset must
not exceed `end`, and may exceed `start` by at most two
seconds (fixed internal limit); otherwise selection fails. The configured start
is never moved. Before selection the pilot continuously delays one page: fetch
N while P is still wholly unissued, classify N, and only then release P if N is
not the target. After all raw P records have been processed (including blanks),
P enters C and N becomes the pending page. No fetch happens during partial
release. If N is T, freeze P/T before any P tuple is handed off. A first-page
target without P fails with pending_missing; there is no single-page fallback.

L=first_second(P), H=last_second(T), V=max(H,int(end)). The final raw C second
must be strictly less than L, even if its text was blank. A shared C/P second
fails left_boundary_committed; arbitrary extra boundary buffering is not used.
Unknown control IDs before L fail unverified_prefix. New IDs inside P, inside
T or in the P/T gap may be merged only after full verification. The gap is not
assumed empty. No P/T row is yielded before verification. `repair_window_verified` records
`configured_start` and positive `start_shift` separately from actual `first`.
Previously emitted prefix rows can already be in the temporary spool; this does
not publish a result. A failed download cannot replace the old ready/current
generation, cache or user history; its own reserved attempt may be marked failed.

The independent control iterator starts by the configured offset and continues
only via its own output cursors. Calls are sequential. Each iterator alone owns
its parent page and original continuation cursor. No control cursor can be
assigned to the main iterator. After checking, consumed main lookahead is emitted
once and that same main iterator resumes after its own last buffered page.

`end` is the minimum right verification boundary, not the repair page end.
Let H be T's actual last second and V = max(H, int(end)). Both control
and main lookahead must observe a second strictly greater than V. This crosses
the whole calendar second of fractional end values too. There is no separate
right drift allowance; the existing query/lookahead budgets decide whether
coverage is possible. H remains the repair boundary. All boundary-second records stay buffered. Control records
after that last second are a checkable tail, not extra output. The main iterator
must cross V within two lookahead pages. A same-second group in
the next main page is held together with the original page. Every control-tail
ID must be matched with identical data in held main continuation. Missing IDs
strictly inside observed coverage cause immediate refusal. Missing IDs on the
last partial second or beyond coverage require more lookahead within the same
two-page budget; otherwise the operation fails. No automatic window expansion
or dropping of an unverified tail occurs.

The verified event additionally records configured_end, repair_high (H),
verification_high (V), main_last_second, control_last_second and
tail_confirmed_ids. right_barrier=true requires both branches strictly past V
and every control-tail ID matched; IDs after H are never automatic additions.

## Identity and order

Union is by Message ID. Different IDs with identical text/author/timestamps stay
distinct. Repeated IDs must have equal exact offsets, original createdAt and a
canonical content fingerprint covering commenter, message and the ordinary
renderer's normalized fragments/text/color/badges. Any conflict fails.

Sorting uses exact offset and UTC timestamp, preserving up to nine fractional
digits of createdAt. Source adjacency forms a partial order. A topological
merge preserves those relationships, including equal timestamps. Its inputs are
the concatenated main C+P+T+Q and the concatenated control pages. Unconstrained
ties use first observation order; lexical Message ID order is never used as
time. Conflicting source order fails. The bounded prefix also participates in
these checks but is never emitted again. Main IDs remain present, matched tail
IDs are emitted only from main, and added control IDs are emitted only inside
the held pair/boundary group. Simple adjacent repeated IDs on P/T can collapse
to one ID. Complex overlaps such as P=[A,B], T=[A,B] at equal keys create an
order cycle in the concatenated input and deliberately fail order_conflict.
No arbitrary pre-deduplication hides that cycle or an [A,B]/[B,A] contradiction.
Only C IDs are removed from output, never P IDs. Existing blank-text filtering still applies;
identity counts and emitted/spooled row counts are distinct.

Received main keys are checked independently of emitted keys, including N vs
unissued P; conflicts on a non-target P/N boundary fail before releasing P.
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
| Committed cache C | 2 main pages |
| Pending previous P | 1 main page |

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

All live C/P/T/B/Q objects count before every request and after role changes.
A role alias of the same BufferedPage counts once; separate raw pages containing
the same ID count separately. The structural maximum is ten pages, not a promise
that ten pages fit the byte/record/estimate guards. Incoming JSON and merge
maps/lists coexist with retained records; dropping Retention references alone
does not prove node cleanup. Limits are unchanged and never automatically raised.

## Failure, cancellation and publication

Fail closed: no fallback to an unverified ordinary result. Invalid boundaries,
prefix/tail gaps, conflicts, order problems, limit exhaustion, Twitch failures,
write errors and cancellation propagate through existing worker failure paths.
EOF before the mandatory window fails window_not_checked, including a final
blank-only P. It is never a successful truncated stream or an implicit flush of
unverified P. Generator close/cancellation at a partial or last yield does not
complete the release/repair stage; owned buffers and both iterators close.
The worker's existing ten-second first-tuple timeout is unchanged. Two valid
six-second main responses can therefore time out this selected pilot; ordinary
downloads have no new delay. ContextVar tokens remain scoped to each request,
so a wait_for first pull and subsequent pulls in another task remain supported.
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

Two-page events add span_pages=2, pending_present, candidate_first, actual L/H/V,
control_prefix_ids (unique observed IDs before L), rejected_unknown_ids (actually
examined unknown C IDs before the immediate refusal), stage and peak retention.
The verified event's first is L; candidate_first is T's first offset second.
pending_original_ids and candidate_original_ids count raw P/T unique IDs;
pair_original_ids counts their union. pair_main_ids also includes main Q records
in H. pair_merged_ids counts the verified [L,H] union; pair_added_ids counts new
control IDs absent from all those main records. Thus a Q continuation of H can
increase pair_merged_ids without being counted as a new control addition.

Additions use disjoint exact-offset bands: added_pending_ids <= last_offset(P),
added_gap_ids between last_offset(P) and first_offset(T), added_target_ids >=
first_offset(T). In a shared offset, the P band takes precedence; these are
metric conventions, not extra restrictions on the chronological merge.
The old original_ids/merged_ids/added_ids remain target-oriented: original T
plus its main H group, then only target-band control additions. They are not
silently replaced by pair counts. Historical T 73->106 and pair 121->154 are
separate assertions. repair_end.verified means full held output was processed;
only outcome=success after main EOF means successful completion of the consumer.

`repair_page_received` includes `ids_sha256` (the full ordered ID sequence using
the existing `ids_hash`), `input_cursor_sha256` and `output_cursor_sha256`.
Branch/page/attempt identify each stream independently. Hashing is skipped
without an active private journal, including after recording stops. Equal
hashes identify equal observed sequences, not absolute Twitch completeness.
