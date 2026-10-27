# TDB — Twitch Downloader Bot

TDB is a Telegram bot that downloads chat messages from public Twitch VODs, processes their metadata and emotes, generates the existing interactive HTML viewer, and sends the resulting link to the user. SQLite stores the VOD cache and user download history.

## Requirements

- Python 3.12 (the initially checked version for a clean installation)
- Network access to the Telegram, Twitch, and optional emote/badge services
- A writable project data directory

Node.js is not required to run the Python bot. The HTML viewer loads its browser-side `html2canvas` dependency from its existing CDN URL.

## Clean installation

Run these commands from the repository root.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv-clean
.\.venv-clean\Scripts\python.exe -m pip install --upgrade pip
.\.venv-clean\Scripts\python.exe -m pip install -r requirements.txt
```

### POSIX shell

```sh
python3.12 -m venv .venv-clean
. .venv-clean/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The `.venv-clean` name is only an example. Do not reuse an existing environment when validating a clean installation.

## Environment variables

The application reads credentials from the environment. It does not automatically load `.env` files.

Required:

- `TELEGRAM_BOT_TOKEN_TWITCH`
- `TWITCH_CLIENT_ID`
- `TWITCH_ACCESS_TOKEN`

`TWITCH_CLIENT_ID` and `TWITCH_ACCESS_TOKEN` are used for authenticated Helix requests. GraphQL requests use the separate optional `TWITCH_GQL_CLIENT_ID`; when it is unset, the application uses the public Client ID TwitchDownloader uses for comments. Setting `TWITCH_GQL_CLIENT_ID` overrides that default and does not affect Helix. This fallback belongs to a third-party application and may stop working or be restricted by Twitch; configure a Client ID you control if that happens.

Optional:

- `TCD_HISTORY_ENABLED` — defaults to `false`; set `true` to enable personal download history (case-insensitive). When disabled, `/history` is hidden from the command menu, manual requests and old history buttons report that history is disabled, and new personal history rows are not recorded. Existing rows and the technical VOD cache are preserved; enabling the setting on restart restores history access.
- `TCD_DB_PATH` — SQLite path. Without it, the database is `vod_history.db` in the project root.
- `TCD_LOG_LEVEL` — logging level; defaults to `INFO`.
- `TCD_HTML_PUBLISH_MODE` — defaults to `local`; local mode saves HTML under `_out` and does not create a public link.
- `TCD_PUBLIC_HTML_DIR`, `TCD_PUBLIC_BASE_URL` — required only when `TCD_HTML_PUBLISH_MODE=public`.
- `TCD_PUBLIC_HTML_URL_PREFIX` — optional public URL path prefix; defaults to `/public_html`.

Use `.env.example` as a list of variable names and configure the real values through the shell, a process manager, or another secret-management mechanism. Never commit real credentials.

## Running

From the repository root:

```powershell
python -m src.main
```

The startup sequence validates required credentials, initializes SQLite, creates the shared HTTP session, registers Telegram handlers, and starts long polling.

Each bot process permits one active VOD download/generation/publication at a time for all users. A second processing request receives a busy message; there is no waiting queue. Selecting a format does not reserve the slot until processing starts. Usable cached results remain available to other users while processing runs. Run only one bot instance: this limit is in memory and does not coordinate multiple processes. Network waits and HTML preparation do not hold the sequential Telegram update handler; resource limits are described below.

## Disk-backed processing (Stage B)

Chat rows and case-sensitive unique usernames are stored in a separate temporary SQLite database for each operation. A bounded buffer is flushed in a worker thread; two sequential cursor passes preserve emote selection and stream the existing viewer into one HTML file. Each pass verifies sequence continuity and the committed message count, aborting publication if temporary data is incomplete. The production path never assembles all rows, the entire JSON, or the HTML string in memory. The original string-returning renderer remains available for small direct callers. Network responses, catalogs, one row and a bounded write buffer still require RAM; a single unusually large row can exceed the buffer byte target. Browser memory remains proportional to the complete chat because the viewer still parses all JSON.

Optional settings:

- `TCD_SPOOL_DIR` — defaults to `<project>/_work`; relative overrides are also project-relative and `~` is expanded. Use a private writable directory on a real disk, separate from public HTML; do not use `/dev/shm` or a RAM disk.
- `TCD_SPOOL_BATCH_MESSAGES` — maximum buffered rows, default `5000`.
- `TCD_SPOOL_BATCH_BYTES` — maximum buffered UTF-8 JSON bytes, default `4194304` (4 MiB). A batch may flush earlier to keep progress counts current; an oversized single row is written alone.

Reserve disk for the temporary SQLite (including its username index/journal) plus the new HTML and all retained finished results. Database and HTML sizes depend on text and metadata; synthetic measurements are not real-chat averages. An `ENOSPC`/write/API failure aborts the job, cleans owned temporary resources, and leaves the previous ready cache entry intact. No finished-page retention or disk-pressure deletion is implemented. A crash after the atomic rename but before cache update can still leave a complete unregistered HTML; generation tracking is deferred to the next stage.

Cancellation waits for any blocking operation to finish or observe its cooperative stop request before resources and the global slot are released. On startup, only correctly named and marked temporary chat directories whose OS lease can be acquired are removed. Unknown files, symlinks, active leases and finished HTML are preserved. Existing publisher temporary files retain their strict naming/24-hour cleanup rule and now also use OS leases. Restarting an interrupted download starts it again from the beginning; there is no cursor resume.

Resource INFO logs report current RSS, process-lifetime peak RSS, Linux process swap, temporary DB size, completed HTML size and elapsed job time. `VmHWM` is a lifetime process peak, not a per-job peak. Windows uses working-set counters and reports swap as unavailable; missing `/proc` is safe. The preliminary 350–400 MiB RSS budget must be validated on the actual Oracle VM; this change does not impose a hard memory limit. Python/GIL, disk I/O and one large API page can still affect latency.

Offline reproduction (no credentials, synthetic rows, fresh process per size):

```sh
python benchmarks/benchmark_streaming.py --sizes 10000 100000 250000
```

The benchmark runs the real download/spool/renderer/local-publisher path with mocked network sources, samples RSS and reports the OS process peak, timings and file sizes. It stops above 700 MiB RSS or below 1 GiB free disk and removes only its own temporary files. `--work-dir /path/on/disk` changes its scratch location. Streaming time includes file writes; publication overhead excludes streaming, and ingestion time includes synthetic generation. Linux `/proc` swap and Windows private bytes have different meanings and are reported separately. Repeat on Ubuntu 24.04/Python 3.12 and measure bot responsiveness, storage permissions, free disk and RSS before serving large VODs.

## HTML result registry and regeneration (Stage D)

`html_results` keeps stable UUID generation IDs, VOD/format identity, metadata/statistics snapshots, configured storage root and filename, reference/URL, byte size, SHA-256, creation/preparation/completion times, lifecycle state and the previous generation. `html_current` has one current ready generation per VOD/format. The existing `vod_cache` is a compatibility mirror updated in the same transaction as the current pointer; personal history remains independent and disabled by default.

A repeated VOD link is checked against the registry before contacting Twitch. A usable result offers **«Выбрать готовую страницу»** and **«Сгенерировать заново»**. Opening rechecks the current generation and its local file, even from an older button and while another download is busy. Regeneration uses the existing global slot, without a queue; a per-user short-lived choice token prevents duplicate or foreign-user clicks from starting work. After a restart, expired regeneration buttons ask for a fresh choice. Missing/unreadable files are not returned as valid links and their records are not deleted; normal creation remains available. In local mode, opening reports the local HTML path instead of creating a public URL or sending a document. HTML has no TTL; the old cache TTL still applies to other formats.

Before work, a `building` generation reserves an exact UUID filename and an OS lease in the work directory. Streaming output is written to an exclusive temporary file. Its actual bytes are hashed incrementally, flushed and synced; size/hash and result metadata are committed as `publishing` before final-file visibility. An atomic same-directory hard link publishes the completed file without overwriting any existing filename, then the temporary name is removed. This requires a filesystem supporting hard links (for example ext4/NTFS); an unsupported operation fails safely instead of falling back to an overwrite. Linux also syncs the output directory. One short SQLite transaction then marks the result `ready`, mirrors the cache and switches the current pointer with a check against the previous generation. Former completed files and previously sent direct URLs remain intact.

Download, serialization, disk, publication or SQLite failures/cancellation leave the previous current result unchanged. Worker threads and cleanup finish before the slot is released. A fully published file left by a failed final transaction is still associated with its registered generation and is retained. A Telegram notification failure after a successful commit does not undo that commit.

Startup recovery visits registered unfinished operations, skipping active OS leases. It verifies expected size, SHA-256 and safe paths using bounded reads. Proven complete files become ready generations; recovery conservatively retains an existing current pointer rather than replacing it. A first publication interrupted in `publishing` may be adopted only if it never had a previous generation and no current result exists. Known failed/cancelled generations are never automatically selected. Incomplete operations become interrupted. Recovery retains abandoned generation temporary files with diagnostics: a reserved name or matching content does not prove ownership after a crash. Registered temporary paths are also excluded from generic age cleanup, regardless of generation state; they require manual investigation. During normal publication, replacement of the created temporary file is checked before linking and cleanup. Unknown finished HTML is never scanned or deleted. Changed/unrecognized paths, inaccessible files and unknown lease markers are retained with diagnostics. Recovery is repeatable and does not resume Twitch cursors.

The first startup automatically adds the two registry tables, indexes and integrity triggers in a transaction. Existing cache/history tables and rows are preserved. Recognizable nonempty legacy HTML under the configured local/public roots becomes a first legacy generation; a bounded header/footer check rejects obviously truncated documents. Unknown references, ambiguous file ownership, incomplete documents and missing files stay explicit unverified records. Legacy files have the old cache's availability evidence, not a newly invented checksum or a full revalidation of embedded JSON. Temporary access failures can be rechecked on a later request. No old HTML is removed during migration.

For an update of the running Oracle bot, the operator should gracefully stop the existing `tdb.service`, preserve a consistent backup of the configured application SQLite (preferably SQLite's backup API), install the new code, and start only one instance. Keep all finished HTML. The first startup may migrate the schema and verify interrupted publications, so allow it to complete; check logs, old ready links and both new buttons afterward. Keep the backup for rollback. These are manual deployment instructions; the development workflow does not connect to the server or restart services.

The registry also supplies the ownership, timestamps, current pointer and predecessor protection used by Stage E below. No HTML TTL or generation-count limit is used.

The offline benchmark can include the registry/hash/commit protocol with `--registry`, for example `python benchmarks/benchmark_streaming.py --registry --sizes 10000 100000`. The stored Stage B measurements describe the earlier publication path; Windows measurements do not certify Oracle/Linux memory use.

## Disk pressure cleanup (Stage E)

After result recovery at startup and then every 10 minutes, the bot checks the filesystem containing the configured publication directory (`TCD_PUBLIC_HTML_DIR` in public mode, project `_out` in local mode). With at least **5 GiB** free it starts no deletions. Below that threshold it considers registered `ready`, `managed` HTML generations, oldest completion/creation first, until at least **8 GiB** is free or no eligible results remain. Free space is remeasured after each deletion. Unknown files, publisher temps and chat spools are not candidates. At sufficient free space all generations are retained indefinitely; under pressure even a current page and its previously sent direct URL can become unavailable.

Settings are `TCD_HTML_CLEANUP_LOW_BYTES=5368709120`, `TCD_HTML_CLEANUP_TARGET_BYTES=8589934592`, and `TCD_HTML_CLEANUP_INTERVAL_SECONDS=600`. All are positive integers and the target must exceed the low threshold. Checks run in an owned worker thread with bounded reads and paged SQLite queries. The periodic task is stored on the application, is not duplicated on reinitialization, and is drained by `post_stop`/`post_shutdown`; an infinite task is deliberately not registered with PTB's `Application.create_task`, which `Application.stop()` waits for.

An additive `html_deletions` journal records the intended path, filesystem identity and a unique private quarantine directory before mutation. The file is verified against its registered size/checksum (legacy files use the existing bounded envelope evidence), atomically renamed within the same filesystem, and checked again. The quarantine directory identity is durable before rename; `quarantined` is committed before unlink. On Linux, unlink is anchored to an opened directory descriptor with no symlink following. One SQLite transaction then marks the generation `deleted`, removes its current pointer if still selected, invalidates only the matching cache URL, and commits the deletion journal. Generation/history rows and other completed HTML remain. `html_removed_heads` records deliberately removed current results, preventing automatic fallback through legacy migration and preventing a stale publication from replacing a subsequently deleted newer head. A fresh ordinary generation clears this marker.

Deletion claims and `Publication.reserve()` serialize their predecessor decisions with `BEGIN IMMEDIATE`. A regeneration reserved before a claim protects its previous ready result. Publication/recovery leases protect active files; a persistent `.<database-name>.html-cleanup.lock` beside SQLite prevents concurrent cleanup passes even if the work directory changes. If a regeneration starts after a deletion claim, its final compare-and-swap accepts only the exact removed predecessor, or the unchanged current predecessor. It never overwrites a newer head. Cleanup recovers journalled operations before considering new pressure-driven work. If space is already at least 5 GiB on restart, a still-present quarantined file is restored without overwriting another file; only an unlink already completed is finalized in SQLite.

Filesystem errors are logged without chat content or credentials and other safe candidates can proceed. Before unlink, rollback restores the original name without overwriting another file where possible. Otherwise the quarantine and journal remain recoverable; ambiguous/substituted files and unproven directories are retained for operator investigation. Finished-file removal is never based on a filename glob. Keep publication/work directories writable only by the service account and restrict other writers; validation is not a defence against a privileged process modifying files between system calls. Logs report deletions, measured free-space change and an unmet target, without periodic INFO noise on a healthy disk. Back up SQLite and HTML before updating the running service; test on Ubuntu 24.04/Python 3.12 before deployment. Development tests simulate disk pressure and delete only disposable fixture files.

## Administrative backup and restore

The standalone standard-library tool `tools/backup_restore.py` creates and verifies a private SQLite/HTML snapshot and restores only into a new directory. It requires an explicitly acknowledged maintenance window with `tdb.service` stopped; it never stops/starts the service or loads the bot's environment. See [backup/restore instructions](docs/backup-restore.md) for source protection, disk reserve, off-VM transfer and independent restore checks. No backup, deployment, or production restore runs automatically.

For an already completed snapshot, `tools/offsite_backup.py` streams an age-encrypted archive to the existing Dropbox remote, verifies its complete contents and only then rotates archives recorded in its private journal. See [offsite backup instructions](docs/offsite-backup.md) for quota admission, safe retries and independent recovery. It never changes production data or manages the bot service.

## Local data and output

- Default SQLite database: `vod_history.db` in the project root.
- `TCD_DB_PATH` can point to an existing database; existing databases are not moved automatically.
- Generated HTML files: `_out/chat_<uuid>.html` in the project root. The completion card reports this local path; it does not send the file or a public link.
- `src/viewer_template.html` is loaded from the source tree and must remain present.

## HTML publication limitation

The default local mode writes generated pages to `_out` and stores a local artifact reference in the existing cache field. Cache hits and history entries are usable while that file remains present. Local files are not served to Telegram users as links and are not sent as documents.

Public publication remains available as an explicit mode, but requires `TCD_PUBLIC_HTML_DIR` and `TCD_PUBLIC_BASE_URL`; that directory must be writable and served externally. No hosting or storage provider is selected or configured by default.

## Ubuntu 24.04 LTS / Python 3.12

Place a complete checkout at `/opt/twitch-downloader-bot`, readable by the service user. Create a new Linux virtual environment; do not copy a Windows environment. The following installation commands use an isolated virtual environment, not the system Python package directory:

```sh
sudo apt update
sudo apt install -y python3.12 python3.12-venv ca-certificates
sudo python3.12 -m venv /opt/twitch-downloader-bot/.venv
sudo /opt/twitch-downloader-bot/.venv/bin/python -m pip install -r /opt/twitch-downloader-bot/requirements.txt
```

For a new installation, create a dedicated service account and writable data directories:

```sh
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/twitch-downloader-bot --shell /usr/sbin/nologin tdb
sudo install -d -o tdb -g tdb -m 0750 /var/lib/twitch-downloader-bot /opt/twitch-downloader-bot/_out /opt/twitch-downloader-bot/_work
```

The service needs read/traverse access to the checkout and virtual environment, and write access to `_out`, `_work` (or the configured `TCD_SPOOL_DIR`) and the database directory. `_out` is currently fixed relative to the checkout and is initialized even in public mode. SQLite also needs permission to create journal files next to the database. Keep the temporary work directory private and on disk; if you override it, create that directory writable by `tdb` before the first download.

Create the environment file only for the initial installation; retain the existing file during updates:

```sh
sudo install -o root -g root -m 0600 /opt/twitch-downloader-bot/.env.example /etc/twitch-downloader-bot.env
sudoedit /etc/twitch-downloader-bot.env
```

Replace the three required credential placeholders listed above. Set these non-secret settings:

```ini
TCD_DB_PATH=/var/lib/twitch-downloader-bot/vod_history.db
TCD_HTML_PUBLISH_MODE=local
TCD_LOG_LEVEL=INFO
```

Use `NAME=value` assignments without `export` or shell substitutions. Use absolute filesystem paths. Systemd loads this file through `EnvironmentFile`; the Python application does not load it itself. An unset `TWITCH_GQL_CLIENT_ID` uses the documented fallback; do not enable its placeholder value.

Example `/etc/systemd/system/tdb.service`:

```ini
[Unit]
Description=Twitch Downloader Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=tdb
Group=tdb
WorkingDirectory=/opt/twitch-downloader-bot
EnvironmentFile=/etc/twitch-downloader-bot.env
ExecStart=/opt/twitch-downloader-bot/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=5s
KillSignal=SIGTERM
TimeoutStopSec=infinity
UMask=0027
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

PTB waits for active downloads before shutting down and closing the shared HTTP session. `TimeoutStopSec=infinity` preserves that behavior, so stopping can take as long as an active download. A finite stop timeout permits systemd to terminate unfinished work when that timeout expires.

After configuring the credentials and saving the unit:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now tdb.service
sudo journalctl -u tdb.service -f
# Graceful stop:
sudo systemctl stop tdb.service
```

The default local mode creates HTML in `/opt/twitch-downloader-bot/_out` without a public link. For public mode, set `TCD_HTML_PUBLISH_MODE=public`, an absolute `TCD_PUBLIC_HTML_DIR`, and a valid `TCD_PUBLIC_BASE_URL`. An optional `TCD_PUBLIC_HTML_URL_PREFIX` overrides `/public_html`; use a leading slash and a base URL without a trailing slash. Create the public directory writable by `tdb`, and grant the separate HTTP server read/traverse access to that directory and its HTML files. The HTTP server must serve that directory at the configured public URL prefix; the bot only writes local files and does not run an HTTP server or upload them. Keep SQLite and finished HTML persistent; no automatic removal of finished pages is configured.

## Checks without starting the bot

For the existing Oracle VM, the manually triggered SSH workflow, dedicated-key
setup, host-key pinning, recovery behavior and operational limits are described
in [Oracle deployment](docs/oracle-deployment.md). Its default mode checks access
without updating or restarting the service. Publishing the workflow or preparing
these files does not deploy the application.

After installation, syntax and import checks can be run without polling:

```powershell
python -m compileall src
python -c "import src.config; import src.twitch_api; print('imports ok')"
```

These checks do not replace tests against Telegram or Twitch, which require configured credentials and external services.
