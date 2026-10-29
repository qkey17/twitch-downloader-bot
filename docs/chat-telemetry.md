# Chat operation telemetry

The existing `tcd.resources` logger emits compact JSON records. Use existing
application logs / `journalctl -u tdb.service` and select this logger; normal
log rotation is unchanged. There are no new files, database writes or monitors.

`operation_start`, `stage_end` and `operation_end` carry the existing 32-character
operation ID. A ContextVar propagates it through async calls and the existing
`asyncio.to_thread` helper. No registry or parallel slots are introduced. Direct
compatibility calls without an admitted operation ID report null rather than
log arbitrary input as an identifier.

Stages:

| Name | Boundary |
| --- | --- |
| result_reserve | Durable generation reservation |
| vod_metadata | Download worker's VOD metadata request |
| messages_spool | Fetch comments, batch-write temporary SQLite and report progress |
| emotes_badges | Existing sequential external asset requests |
| analysis_html_publication | Chat scan, streaming HTML encoding/write, hash, fsync and publication |
| result_commit | Complete generation and existing user-history write |

HTML is written by a callback inside the publisher, so analysis, rendering and
publication are intentionally combined. No publication logic is moved to split
them. The earlier UI metadata lookup before slot admission is outside these
measurements. Normal total time begins on slot admission and ends after runner
cleanup/result delivery attempts; direct runner calls begin at runner entry.
Startup failure and cancellation before runner entry also produce one summary.
Repeated cleanup cannot produce another summary. A cancelled worker thread is
still drained by the existing helper before normal runner finalization.

Fields:

- `elapsed_seconds`: monotonic wall time for the named stage, or total time on
  operation_end. Includes waits inside that boundary, not pure CPU work.
- `process_memory`: RSS, process-lifetime peak RSS, swap where available; on
  Windows existing private-memory reporting is retained. Missing values are
  null. These describe the whole process, not incremental memory of this chat.
- `process_cpu_seconds`: cumulative CPU time of the whole process at the
  checkpoint. Differences also include other threads/tasks; not per-chat CPU.
- `metrics.messages`: durably spooled messages observed by the download worker;
  `spool_bytes`: largest observed spool-directory size including known SQLite
  sidecars/lease; `html_bytes`: output bytes reported by the existing publisher.
  Absent measurements are omitted, not invented as zero.
- `outcome`: success/error/cancelled; `error_code`: safe category only (timeout,
  io_error, invalid_data, empty_chat, cancelled, operation_error). No raw
  exception strings, VOD/user/chat IDs, message content or URLs are included.

The normal operation emits eight records (start, six completed/failed stages,
end). Short/failed operations emit fewer. Existing periodic chat_spooled and
html_published calls update operation metrics instead of adding recurring log
records; legacy calls outside an observed operation retain their old format.
Metric reads and logging failures are best effort and do not fail the work.
There is no per-message system-metric read, new thread, timer or sampling loop.
Expected overhead is a few checkpoint reads and JSON logs, not a measured
production performance guarantee.

Limits: no per-chat RSS peak, no continuous/cgroup memory peak, no separate
network latency/traffic, executor queue time or SQLite lock-wait breakdown, no
guaranteed peak journal/disk allocation, and no pure HTML CPU timing. Cleanup
failures can affect total outcome, and success means the existing runner
returned normally, not proof that a user viewed the result.

For a future one-versus-two comparison, group by operation ID, compare stage
and total distributions/outcomes, and separately monitor process/cgroup peaks,
event-loop latency, disk headroom, swap and network failures. Never sum or
attribute process-wide peaks to individual chats. These measurements alone do
not authorize concurrency: the limit remains one, and separate correctness and
resource trials still require approval.
