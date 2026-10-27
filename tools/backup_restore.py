"""Offline administrative snapshots. Standard library only; no application imports."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import time


FORMAT = "tdb-backup-v1"
BLOCK = 512 * 1024
MAX_MANIFEST = 8 * 1024 * 1024
MAX_FILES = 20000
FINAL = r"chat_[0-9a-f]{32}\.html"
TEMP = rf"\.{FINAL}\.[0-9a-f]{{32}}\.tmp"
QUARANTINE = r"\.tdb-delete-[0-9a-f]{32}-[0-9a-f]{32}"
TABLES = {"vod_cache", "vod_files", "user_history", "html_results",
          "html_current", "html_deletions", "html_removed_heads"}


class BackupError(RuntimeError):
    """A safe refusal; CLI never prints database rows or raw exception details."""


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise BackupError("Use explicit absolute paths without parent traversal")
    return path


def _stamp(info):
    # Windows stat() still exposes creation time as ctime, while fstat() may
    # expose change time. Birth time is the consistent identity field there.
    change = info.st_ctime_ns if os.name == 'posix' else info.st_birthtime_ns
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change]


def _no_links(path):
    for part in (*reversed(path.parents), path):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise BackupError("Symlinks and junctions are not allowed")


@contextmanager
def _open_input(path, directory=False):
    """On POSIX anchor every component to a no-follow directory descriptor."""
    path = _absolute(path)
    fd = None
    try:
        if os.name == "posix":
            fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for index, name in enumerate(path.parts[1:]):
                is_dir = directory or index < len(path.parts) - 2
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if is_dir:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(name, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        else:
            _no_links(path)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise BackupError("Expected a regular file or directory")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _entries(path):
    _no_links(path)
    if os.name == "posix":
        with _open_input(path, directory=True) as fd, os.scandir(fd) as entries:
            for entry in entries:
                yield entry.name, entry.stat(follow_symlinks=False)
    else:
        with os.scandir(path) as entries:
            for entry in entries:
                _no_links(path / entry.name)
                # Windows DirEntry.stat() may omit the volume/file identity.
                yield entry.name, (path / entry.name).lstat()


def _mkdir(path):
    _no_links(path.parent)
    if os.name == "posix":
        with _open_input(path.parent, directory=True) as fd:
            os.mkdir(path.name, mode=0o700, dir_fd=fd)
    else:
        path.mkdir(mode=0o700)


@contextmanager
def _new_file(path):
    _no_links(path.parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    if os.name == "posix":
        with _open_input(path.parent, directory=True) as parent:
            fd = os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    else:
        fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as output:
        yield output
        output.flush()
        os.fsync(output.fileno())


def _sync_directory(path):
    if os.name == "posix":
        with _open_input(path, directory=True) as fd:
            os.fsync(fd)


class _Budget:
    def __init__(self, directory, reserve, timeout):
        if type(reserve) is not int or reserve < 0 or not math.isfinite(timeout) or timeout <= 0:
            raise BackupError("Invalid reserve or timeout")
        self.directory, self.reserve = directory, reserve
        self.deadline = time.monotonic() + timeout

    def check(self, extra=0):
        if time.monotonic() >= self.deadline:
            raise BackupError("Operation timed out; incomplete output must not be used")
        if shutil.disk_usage(self.directory).free < self.reserve + extra:
            raise BackupError("Insufficient free space above the requested reserve")


def _digest(path, expected=None, budget=None, output=None):
    digest, size = hashlib.sha256(), 0
    with _open_input(path) as fd:
        before = _stamp(os.fstat(fd))
        if expected is not None and before != expected:
            raise BackupError("Source changed before copying")
        while True:
            if budget:
                budget.check(BLOCK if output is not None else 0)
            chunk = os.read(fd, BLOCK)
            if not chunk:
                break
            if output is not None and output.write(chunk) != len(chunk):
                raise BackupError("Incomplete output write")
            digest.update(chunk)
            size += len(chunk)
        if _stamp(os.fstat(fd)) != before or _stamp(path.lstat()) != before or size != before[2]:
            raise BackupError("Source changed while reading")
    return dict(size=size, sha256=digest.hexdigest(), identity=before)


def _copy(source, target, expected, budget):
    with _new_file(target) as output:
        return _digest(source, expected, budget, output)


def _write(path, content, budget):
    budget.check(len(content))
    with _new_file(path) as output:
        if output.write(content) != len(content):
            raise BackupError("Incomplete metadata write")


def _read_small(path, limit):
    with _open_input(path) as fd:
        if os.fstat(fd).st_size > limit:
            raise BackupError("Metadata exceeds the supported size")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(BLOCK, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        value = b''.join(chunks)
        if len(value) > limit:
            raise BackupError("Metadata exceeds the supported size")
        return value


def _scan(root, budget):
    files, directories = {}, {"": _stamp(root.lstat())}
    for name, info in _entries(root):
        budget.check()
        if re.fullmatch(rf"{FINAL}|{TEMP}", name):
            if not stat.S_ISREG(info.st_mode):
                raise BackupError("An HTML candidate is not a regular file")
            files[name] = _stamp(info)
        elif re.fullmatch(QUARANTINE, name):
            if not stat.S_ISDIR(info.st_mode):
                raise BackupError("A quarantine is not a regular directory")
            directories[name] = _stamp(info)
            for child, child_info in _entries(root / name):
                if child == "page.html":
                    if not stat.S_ISREG(child_info.st_mode):
                        raise BackupError("A quarantined HTML is not a regular file")
                    files[name + "/page.html"] = _stamp(child_info)
        if len(files) + len(directories) > MAX_FILES:
            raise BackupError("Too many HTML entries for this tool")
    return files, directories


def _require_stopped(service):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service", service):
        raise BackupError("Invalid service name")
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", service, "--no-pager",
             "--property=LoadState,ActiveState,SubState,MainPID,Job"],
            capture_output=True, text=True, timeout=5, env={"PATH": os.defpath, "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError("Cannot confirm that the service is stopped") from None
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (result.returncode or fields.get("LoadState") != "loaded"
            or fields.get("ActiveState") != "inactive" or fields.get("SubState") != "dead"
            or fields.get("MainPID") != "0" or fields.get("Job") not in ("", "0")):
        raise BackupError("Maintenance requires a loaded inactive/dead service, PID 0 and no queued job")


def _sqlite_copy(stage, target, budget):
    # SQLite touches only the private clone: even WAL/SHM recovery cannot write to production.
    with _new_file(target):
        pass
    source = sqlite3.connect(stage, timeout=1)
    try:
        destination = sqlite3.connect(target, timeout=1)
        try:
            source.backup(destination, pages=256, progress=lambda *_: budget.check(), sleep=0.05)
        finally:
            destination.close()
    finally:
        source.close()
    _no_links(target)
    # Windows requires a writable handle for FlushFileBuffers. This is only
    # the newly created private destination, never the production database.
    with target.open('r+b') as output:
        os.fsync(output.fileno())


@contextmanager
def _database(path, budget):
    _no_links(path)
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA cache_size=-2048")
        con.set_progress_handler(lambda: (budget.check(), 0)[1], 10000)
        objects = list(con.execute("SELECT name,type FROM sqlite_schema WHERE type IN ('table','view')"))
        if ({row['name'] for row in objects if not row['name'].startswith('sqlite_')} != TABLES
                or any(row['type'] != 'table' for row in objects)):
            raise BackupError("Unsupported database schema; no migration is performed")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("SQLite integrity check failed")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite foreign key check failed")
        yield con
    finally:
        con.close()


def _validate_registry(directory, manifest, budget):
    files = {entry['path']: entry for entry in manifest['files']}
    dirs = {entry['path']: entry for entry in manifest['directories']}
    roots = {value: f"html/{i}/" for i, value in enumerate(manifest['roots'])}
    with _database(directory / "database.sqlite3", budget) as con:
        invalid = con.execute("""SELECT 1 FROM html_current h
            JOIN html_results r ON r.id=h.result_id
            LEFT JOIN vod_cache c ON c.id=r.cache_id
            WHERE r.state!='ready' OR h.vod_id IS NOT r.vod_id OR h.fmt IS NOT r.fmt
              OR c.id IS NULL OR c.vod_id IS NOT r.vod_id OR c.fmt IS NOT r.fmt
              OR c.html_url IS NOT r.html_ref LIMIT 1""").fetchone()
        if invalid:
            raise BackupError("Current result and cache are inconsistent")
        for row in con.execute("SELECT * FROM html_results"):
            budget.check()
            if not row['managed']:
                if row['state'] == 'ready':
                    raise BackupError("A ready result is not covered by explicit HTML roots")
                continue  # Preserve external/unverified references as metadata only.
            prefix = roots.get(row['root'])
            if (prefix is None or not isinstance(row['filename'], str)
                    or not re.fullmatch(FINAL, row['filename'])
                    or (row['origin'] == 'generated' and row['filename'] != f"chat_{row['id']}.html")):
                raise BackupError("Registry path is unsafe or outside explicit HTML roots")
            intent = con.execute("SELECT * FROM html_deletions WHERE result_id=?", (row['id'],)).fetchone()
            active = intent is not None and intent['phase'] in ('pending', 'quarantined')
            candidates = [files.get(prefix + row['filename'])]
            quarantine_dir = None
            if active:
                quarantine = intent['quarantine']
                if (intent['root'] != row['root'] or intent['filename'] != row['filename']
                        or not isinstance(quarantine, str) or not re.fullmatch(QUARANTINE, quarantine)
                        or not quarantine.startswith(f".tdb-delete-{row['id']}-")):
                    raise BackupError("Unsafe deletion journal path")
                quarantine_dir = dirs.get(prefix + quarantine)
                candidates.append(files.get(prefix + quarantine + "/page.html"))
                if quarantine_dir and intent['dir_inode'] is not None:
                    identity = quarantine_dir['identity']
                    if (str(identity[0]), str(identity[1])) != (intent['dir_device'], intent['dir_inode']):
                        raise BackupError("Quarantine identity does not match its journal")
            if row['state'] != 'ready':
                continue  # Interrupted output is preserved, never adopted or repaired.
            present = [entry for entry in candidates if entry is not None]
            if not present:
                # Unlink committed to the FS but not yet finalized in SQLite.
                if not (active and intent['phase'] == 'quarantined'
                        and intent['dir_inode'] is not None and quarantine_dir is not None):
                    raise BackupError("A required ready HTML is missing")
            for entry in present:
                if ((row['size_bytes'] is not None and row['size_bytes'] != entry['size'])
                        or (row['sha256'] and row['sha256'] != entry['sha256'])):
                    raise BackupError("HTML does not match the registered size/checksum")
                if active:
                    identity = entry['identity']
                    if ((str(identity[0]), str(identity[1]), entry['size'], str(identity[3]))
                            != (intent['device'], intent['inode'], intent['size_bytes'], intent['mtime_ns'])):
                        raise BackupError("Deletion file identity does not match its journal")


def _overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def create_backup(database, html_roots, destination, *, maintenance_stopped=False,
                  service="tdb.service", reserve_bytes=1024**3, timeout=300):
    if not maintenance_stopped:
        raise BackupError("Explicit --maintenance-stopped acknowledgement is required")
    _require_stopped(service)
    database, destination = _absolute(database), _absolute(destination)
    if database.suffix.lower() not in ('.db', '.sqlite', '.sqlite3'):
        raise BackupError("Source must be an explicit .db, .sqlite or .sqlite3 file")
    roots = [_absolute(value) for value in html_roots]
    if not roots or len(roots) > 16 or len(set(roots)) != len(roots):
        raise BackupError("Supply 1..16 distinct HTML roots")
    for path in [database, destination.parent, *roots]:
        _no_links(path)
    if any(_overlap(a, b) for i, a in enumerate(roots) for b in roots[i+1:]):
        raise BackupError("HTML roots must not overlap")
    if any(_overlap(destination, path) for path in [database, *roots]):
        raise BackupError("Destination overlaps source data")
    budget = _Budget(destination.parent, reserve_bytes, timeout)
    scans, count = [], 2  # database.sqlite3 and the top-level html directory
    for root in roots:
        scan = _scan(root, budget)
        count += sum(map(len, scan))
        if count > MAX_FILES:
            raise BackupError("Too many HTML entries for this tool")
        scans.append(scan)
    raw = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(database) + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not suffix:
                raise BackupError("Source SQLite is missing") from None
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("SQLite or sidecar is not a regular file")
        raw[suffix] = _stamp(info)
    estimated = 2 * sum(value[2] for value in raw.values()) + MAX_MANIFEST
    estimated += sum(value[2] for files, _ in scans for value in files.values())
    budget.check(estimated)
    _require_stopped(service)
    _mkdir(destination)  # Exclusive; failed attempts remain private and incomplete.
    stage = destination / "_sqlite_stage"
    _mkdir(stage)
    raw_hashes = {}
    for suffix, identity in raw.items():
        if suffix == "-shm":
            # SHM is a transient index; let SQLite rebuild it in the clone.
            raw_hashes[suffix] = _digest(Path(str(database)+suffix), identity, budget)['sha256']
        else:
            entry = _copy(Path(str(database)+suffix), stage/("source.sqlite3"+suffix), identity, budget)
            raw_hashes[suffix] = entry['sha256']
    _sqlite_copy(stage/"source.sqlite3", destination/"database.sqlite3", budget)
    db_entry = _digest(destination/"database.sqlite3", budget=budget)
    db_entry.update(path="database.sqlite3", identity=None)
    manifest = dict(format=FORMAT, source_database=str(database), roots=list(map(str, roots)),
                    files=[db_entry], directories=[dict(path="html", identity=None)])
    _mkdir(destination/"html")
    for index, (root, (files, directories)) in enumerate(zip(roots, scans)):
        prefix = f"html/{index}"
        for name, identity in sorted(directories.items()):
            member = prefix + ("/"+name if name else "")
            _mkdir(destination/member)
            manifest['directories'].append(dict(path=member, identity=identity))
        for name, identity in sorted(files.items()):
            member = prefix + "/" + name
            entry = _copy(root/name, destination/member, identity, budget)
            entry['path'] = member
            manifest['files'].append(entry)
    _validate_registry(destination, manifest, budget)
    # Check DB, sidecars, HTML contents and membership again before completion.
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(database)+suffix)
        if suffix in raw:
            if _digest(path, raw[suffix], budget)['sha256'] != raw_hashes[suffix]:
                raise BackupError("SQLite source changed during backup")
        elif path.exists() or path.is_symlink():
            raise BackupError("A SQLite sidecar appeared during backup")
    for index, (root, scan) in enumerate(zip(roots, scans)):
        if _scan(root, budget) != scan:
            raise BackupError("HTML membership changed during backup")
        for entry in manifest['files']:
            prefix = f"html/{index}/"
            if entry['path'].startswith(prefix):
                relative = entry['path'][len(prefix):]
                if _digest(root/relative, entry['identity'], budget)['sha256'] != entry['sha256']:
                    raise BackupError("HTML source changed during backup")
    _require_stopped(service)
    # Only known files in this newly created private directory may be removed.
    if stage.parent != destination or not stage.is_relative_to(destination):
        raise BackupError("Unsafe staging directory")
    for name, info in _entries(stage):
        if name not in {"source.sqlite3"+s for s in ("", "-wal", "-shm", "-journal")} or not stat.S_ISREG(info.st_mode):
            raise BackupError("Unexpected SQLite staging artifact")
        (stage/name).unlink()
    stage.rmdir()
    content = json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8")
    if len(content) > MAX_MANIFEST:
        raise BackupError("Manifest exceeds the supported size")
    _write(destination/"manifest.json", content, budget)
    for entry in reversed(manifest['directories']):
        _sync_directory(destination/entry['path'])
    _write(destination/"COMPLETE", (hashlib.sha256(content).hexdigest()+"\n").encode("ascii"), budget)
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return verify_backup(destination, _budget=budget)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("Duplicate manifest key")
        result[key] = value
    return result


def _manifest(directory):
    content = _read_small(directory/"manifest.json", MAX_MANIFEST)
    complete = _read_small(directory/"COMPLETE", 65)
    if complete != (hashlib.sha256(content).hexdigest()+"\n").encode("ascii"):
        raise BackupError("Incomplete backup or manifest checksum mismatch")
    try:
        manifest = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise BackupError("Invalid manifest JSON") from None
    if (not isinstance(manifest, dict) or set(manifest) !=
            {'format','source_database','roots','files','directories'} or manifest['format'] != FORMAT
            or not isinstance(manifest['source_database'], str) or not manifest['source_database']
            or '\x00' in manifest['source_database']
            or not isinstance(manifest['roots'], list) or not 1 <= len(manifest['roots']) <= 16
            or any(not isinstance(root, str) or not root or '\x00' in root for root in manifest['roots'])
            or len(set(manifest['roots'])) != len(manifest['roots'])):
        raise BackupError("Unsupported manifest format")
    seen = set()
    for category in ('files', 'directories'):
        entries = manifest[category]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
            raise BackupError("Invalid manifest entry count")
        for entry in entries:
            fields = {'path','identity'} | ({'size','sha256'} if category == 'files' else set())
            if not isinstance(entry, dict) or set(entry) != fields or not isinstance(entry['path'], str):
                raise BackupError("Invalid manifest entry")
            member = entry['path']
            grammar = (rf"database\.sqlite3|html/[0-9]+/(?:{FINAL}|{TEMP}|{QUARANTINE}/page\.html)"
                       if category == 'files' else rf"html(?:/[0-9]+(?:/{QUARANTINE})?)?")
            if member in seen or not re.fullmatch(grammar, member):
                raise BackupError("Unsafe or duplicate manifest path")
            parts = PurePosixPath(member).parts
            if len(parts) > 1 and (str(int(parts[1])) != parts[1] or int(parts[1]) >= len(manifest['roots'])):
                raise BackupError("Invalid manifest root index")
            seen.add(member)
            identity = entry['identity']
            if identity is None:
                if member not in ('html','database.sqlite3'):
                    raise BackupError("Missing source identity")
            elif (not isinstance(identity, list) or len(identity) != 5
                  or any(type(n) is not int for n in identity) or any(n < 0 for n in identity[:3])):
                raise BackupError("Invalid source identity")
            if category == 'files' and (type(entry['size']) is not int or entry['size'] < 0
                    or not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
                raise BackupError("Invalid file size/checksum")
    if 'database.sqlite3' not in seen or 'html' not in seen:
        raise BackupError("Missing mandatory backup entries")
    return manifest


def _check_tree(directory, manifest):
    expected_files = {entry['path'] for entry in manifest['files']} | {'manifest.json', 'COMPLETE'}
    expected_dirs = {entry['path'] for entry in manifest['directories']}
    seen_files, seen_dirs = set(), set()
    for relative in ['', *sorted(expected_dirs)]:
        for name, info in _entries(directory/relative):
            member = relative + '/' + name if relative else name
            if member in expected_files and stat.S_ISREG(info.st_mode):
                seen_files.add(member)
            elif member in expected_dirs and stat.S_ISDIR(info.st_mode):
                seen_dirs.add(member)
            else:
                raise BackupError("Unexpected or unsafe backup member")
    if seen_files != expected_files or seen_dirs != expected_dirs:
        raise BackupError("Backup member is missing")


def verify_backup(directory, *, reserve_bytes=0, timeout=300, _budget=None):
    directory = _absolute(directory)
    _no_links(directory)
    budget = _budget or _Budget(directory, reserve_bytes, timeout)
    manifest = _manifest(directory)
    _check_tree(directory, manifest)
    for entry in manifest['files']:
        actual = _digest(directory/entry['path'], budget=budget)
        if any(actual[key] != entry[key] for key in ('size','sha256')):
            raise BackupError("Backup file checksum mismatch")
    _validate_registry(directory, manifest, budget)
    return manifest


def restore_backup(backup, destination, *, reserve_bytes=1024**3, timeout=300):
    backup, destination = _absolute(backup), _absolute(destination)
    budget = _Budget(destination.parent, reserve_bytes, timeout)
    manifest = verify_backup(backup, _budget=budget)
    originals = [Path(value) for value in [manifest['source_database'], *manifest['roots']]]
    if _overlap(destination, backup) or any(path.is_absolute() and _overlap(destination, path) for path in originals):
        raise BackupError("Restore destination overlaps backup or original data")
    budget.check(sum(entry['size'] for entry in manifest['files']) + MAX_MANIFEST)
    _mkdir(destination)
    for entry in sorted(manifest['directories'], key=lambda entry: entry['path'].count('/')):
        _mkdir(destination/entry['path'])
    for entry in manifest['files']:
        actual = _copy(backup/entry['path'], destination/entry['path'], None, budget)
        if any(actual[key] != entry[key] for key in ('size','sha256')):
            raise BackupError("Backup changed while restoring")
    if _manifest(backup) != manifest:
        raise BackupError("Manifest changed while restoring")
    for name in ('manifest.json', 'COMPLETE'):
        _write(destination/name, _read_small(backup/name, MAX_MANIFEST), budget)
    for entry in reversed(manifest['directories']):
        _sync_directory(destination/entry['path'])
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return verify_backup(destination, _budget=budget)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('backup', help='Requires a stopped service for the whole maintenance window')
    create.add_argument('--database', required=True, type=Path)
    create.add_argument('--html-root', required=True, type=Path, action='append')
    create.add_argument('--destination', required=True, type=Path)
    create.add_argument('--maintenance-stopped', action='store_true')
    create.add_argument('--service', default='tdb.service')
    verify = commands.add_parser('verify')
    verify.add_argument('--backup', required=True, type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('--backup', required=True, type=Path)
    restore.add_argument('--destination', required=True, type=Path)
    for command in (create, restore):
        command.add_argument('--reserve-bytes', type=int, default=1024**3)
    for command in (create, verify, restore):
        command.add_argument('--timeout-seconds', type=float, default=300)
    args = parser.parse_args(argv)
    try:
        if args.command == 'backup':
            manifest = create_backup(args.database, args.html_root, args.destination,
                maintenance_stopped=args.maintenance_stopped, service=args.service,
                reserve_bytes=args.reserve_bytes, timeout=args.timeout_seconds)
        elif args.command == 'verify':
            manifest = verify_backup(args.backup, timeout=args.timeout_seconds)
        else:
            manifest = restore_backup(args.backup, args.destination,
                reserve_bytes=args.reserve_bytes, timeout=args.timeout_seconds)
    except (BackupError, OSError, sqlite3.Error, ValueError) as error:
        message = str(error) if isinstance(error, BackupError) else 'I/O, SQLite or input validation failure'
        parser.exit(1, 'ERROR: '+message+'; any incomplete destination is retained, never merged or overwritten.\n')
    print(json.dumps(dict(status='ok', command=args.command, format=FORMAT,
                         files=len(manifest['files']), bytes=sum(e['size'] for e in manifest['files']))))


if __name__ == '__main__':
    main()
