"""Space-pressure deletion of registered HTML, with a durable per-file journal.

Only a registered ready file is eligible. A private same-filesystem quarantine
allows us to inspect what rename actually moved before an irreversible unlink.
No glob deletion, recursive removal, or temporary/spool cleanup lives here.
"""
import asyncio
import hashlib
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from src import config, html_publisher
from src.database import db_core
from src.database import db_results as results
from src.database.db_core import _connect
from src.file_lock import try_lock_file
from src.workers.worker_io import run_blocking

log = logging.getLogger(__name__)
_APP_KEY = 'html_cleanup'


class UnsafeDeletion(ValueError):
    pass


def _identity(info):
    return str(info.st_dev), str(info.st_ino)


def _matches(path, intent):
    return _matches_info(path.lstat(), intent)


def _matches_info(info, intent):
    return (stat.S_ISREG(info.st_mode) and _identity(info) == (intent['device'], intent['inode'])
            and info.st_size == intent['size_bytes'] and str(info.st_mtime_ns) == intent['mtime_ns'])


def _sync(directory):
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _paths(row, intent):
    source = results.result_path(row)
    if (source is None or row['state']!='ready' or row['fmt']!='html_online' or not results._reference_matches(row)
            or intent['root'] != str(source.parent) or intent['filename'] != source.name
            or not re.fullmatch(r'\.tdb-delete-[0-9a-f]{32}-[0-9a-f]{32}', intent['quarantine'])
            or not intent['quarantine'].startswith(f".tdb-delete-{row['id']}-")):
        raise UnsafeDeletion('Invalid deletion path')
    directory = source.parent / intent['quarantine']
    if directory.is_symlink():
        raise UnsafeDeletion('Quarantine is a symbolic link')
    return source, directory, directory/'page.html'


def _owned_directory(directory, intent):
    info = directory.lstat()
    return (stat.S_ISDIR(info.st_mode)
            and _identity(info) == (intent['dir_device'], intent['dir_inode'])
            and directory.resolve().parent == directory.parent)


def _unlink_owned(directory, intent):
    """Recheck after SQLite waits, and anchor Linux unlink to the opened directory."""
    if not _owned_directory(directory, intent):
        raise UnsafeDeletion('Quarantine identity changed before unlink')
    if os.name == 'nt':
        target = directory/'page.html'
        if not _matches(target, intent):
            raise UnsafeDeletion('Quarantined file changed before unlink')
        target.unlink()
        _sync(directory)
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != (intent['dir_device'],intent['dir_inode']):
            raise UnsafeDeletion('Quarantine directory replaced before unlink')
        info = os.stat('page.html', dir_fd=fd, follow_symlinks=False)
        if not _matches_info(info, intent):
            raise UnsafeDeletion('Quarantined file changed before unlink')
        os.unlink('page.html', dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def _cancelled(stop):
    return stop is not None and stop.is_set()


@contextmanager
def _result_lease(row):
    key = row['id']
    if (not isinstance(key, str) or not results._KEY.fullmatch(key)
            or (row['origin'] == 'generated' and row['lease_root'] != str(results._lease_path(key).parent))):
        yield None
        return
    path = results._lease_path(key)
    handle = None if path.is_symlink() else results._recovery_lease(key)
    try:
        yield handle
    finally:
        if handle is not None:
            handle.close()
            path.unlink(missing_ok=True)


def _protected(con, key):
    # reserve() and this check both run under BEGIN IMMEDIATE.
    if con.execute("SELECT 1 FROM html_results WHERE previous_id=? AND state IN ('building','publishing')", (key,)).fetchone():
        return True
    # A completed/failed worker still owns its predecessor until close().
    for child in con.execute("SELECT id,lease_root FROM html_results WHERE previous_id=? AND origin='generated'", (key,)):
        path = results._lease_path(child['id'])
        if child['lease_root'] != str(path.parent) or path.is_symlink():
            return True
        try:
            with path.open('r+b') as handle:
                if not try_lock_file(handle):
                    return True
        except FileNotFoundError:
            continue
    return False


def _sample(row, stop):
    path = results.result_path(row)
    if path is None or not results._reference_matches(row):
        raise UnsafeDeletion('Unmanaged HTML')
    before = path.lstat()
    fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'r+b') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or _identity(info) != _identity(before)
                or info.st_size <= 0 or info.st_size != row['size_bytes'] or not try_lock_file(source)):
            raise UnsafeDeletion('Changed HTML')
        if row['sha256']:
            digest = hashlib.sha256()
            for chunk in iter(lambda:source.read(65536), b''):
                if _cancelled(stop):
                    return None
                digest.update(chunk)
            if digest.hexdigest() != row['sha256']:
                raise UnsafeDeletion('HTML checksum mismatch')
        elif row['origin'] == 'legacy':
            prefix = source.read(4096).lstrip(b'\xef\xbb\xbf \t\r\n').lower()
            source.seek(max(0, info.st_size-4096))
            if not (prefix.startswith((b'<!doctype html', b'<html')) and source.read(4096).rstrip().lower().endswith(b'</html>')):
                raise UnsafeDeletion('Invalid legacy HTML')
        else:
            raise UnsafeDeletion('Missing HTML proof')
        after = os.fstat(source.fileno())
        if (_identity(after), after.st_size, after.st_mtime_ns) != (_identity(info), info.st_size, info.st_mtime_ns):
            raise UnsafeDeletion('HTML changed during verification')
    return info


def _claim(row, info):
    con = _connect()
    try:
        with con:
            con.execute('BEGIN IMMEDIATE')
            current = con.execute('SELECT * FROM html_results WHERE id=?', (row['id'],)).fetchone()
            if (not current or dict(current) != row or row['state'] != 'ready' or not row['managed'] or row['fmt']!='html_online'
                    or _protected(con, row['id'])):
                return None
            prior = con.execute('SELECT phase FROM html_deletions WHERE result_id=?', (row['id'],)).fetchone()
            if prior and prior['phase'] != 'cancelled':
                return None
            now = time.time()
            con.execute("""INSERT INTO html_deletions(result_id,phase,root,filename,quarantine,device,inode,
                size_bytes,mtime_ns,created_at,updated_at) VALUES(?,'pending',?,?,?,?,?,?,?,?,?)
                ON CONFLICT(result_id) DO UPDATE SET phase='pending',root=excluded.root,filename=excluded.filename,
                quarantine=excluded.quarantine,device=excluded.device,inode=excluded.inode,size_bytes=excluded.size_bytes,
                mtime_ns=excluded.mtime_ns,dir_device=NULL,dir_inode=NULL,created_at=excluded.created_at,
                updated_at=excluded.updated_at,error_code=NULL""",
                (row['id'],row['root'],row['filename'],f".tdb-delete-{row['id']}-{uuid.uuid4().hex}",
                 *_identity(info),info.st_size,str(info.st_mtime_ns),now,now))
        return dict(con.execute('SELECT * FROM html_deletions WHERE result_id=?', (row['id'],)).fetchone())
    finally:
        con.close()


def _update(key, **values):
    con = _connect()
    try:
        with con:
            values['updated_at'] = time.time()
            con.execute('UPDATE html_deletions SET '+','.join(f'{name}=?' for name in values)+' WHERE result_id=?',
                        (*values.values(), key))
    finally:
        con.close()


def _finalize(row):
    con = _connect()
    try:
        with con:
            con.execute('BEGIN IMMEDIATE')
            intent = con.execute('SELECT * FROM html_deletions WHERE result_id=?', (row['id'],)).fetchone()
            if not intent or intent['phase'] != 'quarantined':
                raise UnsafeDeletion('Deletion has not reached unlink')
            current = con.execute('SELECT root,filename,state FROM html_results WHERE id=?', (row['id'],)).fetchone()
            if (not current or current['state']!='ready'
                    or (current['root'],current['filename']) != (intent['root'],intent['filename'])):
                raise UnsafeDeletion('Generation changed during deletion')
            # Do not erase a newer generation/cache written during this deletion.
            removed_head = con.execute('DELETE FROM html_current WHERE result_id=?', (row['id'],)).rowcount
            if removed_head:
                con.execute('''INSERT INTO html_removed_heads(vod_id,fmt,result_id) VALUES(?,?,?)
                    ON CONFLICT(vod_id,fmt) DO UPDATE SET result_id=excluded.result_id''',
                    (row['vod_id'],row['fmt'],row['id']))
            con.execute("UPDATE html_results SET state='deleted' WHERE id=? AND state='ready'", (row['id'],))
            con.execute('UPDATE vod_cache SET html_url=NULL WHERE id=? AND html_url=?', (row['cache_id'],row['html_ref']))
            con.execute("UPDATE html_deletions SET phase='done',updated_at=?,error_code=NULL WHERE result_id=?", (time.time(),row['id']))
    finally:
        con.close()


def _remove_empty_directory(directory, intent):
    try:
        if _owned_directory(directory, intent):
            directory.rmdir()  # Never recursive: unknown files always survive.
            _sync(directory.parent)
    except OSError:
        pass


def _restore(row, intent):
    """Best-effort no-clobber rollback before unlink; keep proof if it fails."""
    source, directory, target = _paths(row, intent)
    if source.exists() or source.is_symlink():
        if _matches(source, intent):
            if target.exists() or target.is_symlink():
                if not _owned_directory(directory, intent) or not _matches(target, intent):
                    return
                _unlink_owned(directory, intent)
            _update(row['id'], phase='cancelled')
            _remove_empty_directory(directory, intent)
        return
    if (_owned_directory(directory, intent) and _matches(target, intent)):
        os.link(target, source, follow_symlinks=False)
        _sync(source.parent)
        _unlink_owned(directory, intent)
        _update(row['id'], phase='cancelled')
        _remove_empty_directory(directory, intent)


def _resume(row, intent, stop=None, allow_delete=True):
    source, directory, target = _paths(row, intent)
    if intent['phase']=='quarantined' and intent['dir_inode'] is not None and not target.exists() and not target.is_symlink():
        if not _owned_directory(directory, intent):
            raise UnsafeDeletion('Quarantine identity changed')
        if source.exists() and _matches(source, intent):
            _update(row['id'], phase='cancelled')
            _remove_empty_directory(directory, intent)
            return False
        _sync(directory)
        _finalize(row)  # Already unlinked: finish metadata even on a healthy disk.
        _remove_empty_directory(directory, intent)
        return True
    if not allow_delete or _cancelled(stop):
        _restore(row, intent)
        return False
    if intent['dir_inode'] is None:
        directory.mkdir(mode=0o700)  # Exclusive: a pre-existing directory is not ours.
        info = directory.lstat()
        _update(row['id'], dir_device=str(info.st_dev), dir_inode=str(info.st_ino))
        intent = dict(intent, dir_device=str(info.st_dev), dir_inode=str(info.st_ino))
        _sync(source.parent)
    if not _owned_directory(directory, intent):
        raise UnsafeDeletion('Quarantine identity changed')
    if source.exists() and _matches(source, intent) and target.exists():
        _restore(row, intent)  # Interrupted rollback may have left two hard links.
        return False
    if not target.exists() and not target.is_symlink():
        if not _matches(source, intent):
            raise UnsafeDeletion('Source identity changed')
        os.rename(source, target)
        _sync(directory)
        _sync(source.parent)
    # Inspect the inode actually moved, including races at rename().
    if not _matches(target, intent) or source.exists() or source.is_symlink():
        raise UnsafeDeletion('Ambiguous quarantined file')
    _update(row['id'], phase='quarantined')  # Durable before irreversible unlink.
    intent = dict(intent, phase='quarantined')
    if _cancelled(stop):
        _restore(row, intent)
        return False
    _unlink_owned(directory, intent)
    _finalize(row)
    _remove_empty_directory(directory, intent)
    return True


def _attempt(row, intent=None, stop=None, allow_delete=True):
    try:
        with _result_lease(row) as lease:
            if lease is None:
                return False
            if intent is None:
                info = _sample(row, stop)
                if info is None:
                    return False
                intent = _claim(row, info)
            if intent is None:
                return False
            try:
                return _resume(row, intent, stop, allow_delete)
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                # Re-read the durable directory proof if failure followed mkdir.
                con = _connect()
                try:
                    durable = dict(con.execute('SELECT * FROM html_deletions WHERE result_id=?', (row['id'],)).fetchone())
                finally:
                    con.close()
                _update(row['id'], error_code=type(exc).__name__)
                try:
                    _restore(row, durable)
                except (OSError, ValueError, TypeError, RuntimeError):
                    pass
                raise
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        log.warning('HTML deletion deferred: id=%s error=%s',row['id'],type(exc).__name__)
        return False


def _free(root):
    while not root.exists() and root != root.parent:
        root = root.parent
    return shutil.disk_usage(root).free


def _one(con, sql, params=()):
    # No live read cursor may span a write on another SQLite connection.
    cursor = con.execute(sql, params)
    try:
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        cursor.close()


def run_cleanup(stop=None):
    """One bounded-memory pass. Tests replace _free(), never fill real disks."""
    # Shared by all users of this DB even if SPOOL_DIR changed between runs.
    database = Path(db_core.DB_PATH).expanduser().resolve()
    lock = database.parent/f'.{database.name}.html-cleanup.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.is_symlink():
        return
    with lock.open('a+b') as handle:
        if not try_lock_file(handle):
            return
        con = _connect()
        try:
            after = ''
            while True:
                row = _one(con, """SELECT r.* FROM html_results r JOIN html_deletions d ON d.result_id=r.id
                    WHERE d.phase IN ('pending','quarantined') AND r.id>? ORDER BY r.id LIMIT 1""", (after,))
                if row is None:
                    break
                after = row['id']
                if _cancelled(stop):
                    return
                intent = _one(con, 'SELECT * FROM html_deletions WHERE result_id=?', (row['id'],))
                log.info('Recovering interrupted HTML deletion: id=%s',row['id'])
                source = results.result_path(row)
                # No new physical deletion above the low threshold. Finish an
                # unlink already done, or roll a recoverable rename back instead.
                allow_delete = source is not None and _free(source.parent) < config.HTML_CLEANUP_LOW_BYTES
                _attempt(row, intent, stop, allow_delete=allow_delete)
            configured = html_publisher.PUBLIC_HTML_DIR if config.HTML_PUBLISH_MODE == 'public' else html_publisher.OUT_DIR
            if not configured:
                return
            root = Path(configured).expanduser().resolve()
            before = free = _free(root)
            if free >= config.HTML_CLEANUP_LOW_BYTES or _cancelled(stop):
                return
            log.info('HTML disk cleanup started: free_bytes=%s target_bytes=%s',free,config.HTML_CLEANUP_TARGET_BYTES)
            count = 0
            after = None
            while True:
                if free >= config.HTML_CLEANUP_TARGET_BYTES or _cancelled(stop):
                    break
                page = '' if after is None else ' AND (COALESCE(completed_at,created_at),created_at,id)>(?,?,?)'
                row = _one(con, """SELECT r.* FROM html_results r WHERE state='ready' AND managed=1 AND fmt='html_online' AND root=?
                    AND NOT EXISTS(SELECT 1 FROM html_deletions d WHERE d.result_id=r.id AND d.phase!='cancelled')"""
                    +page+' ORDER BY COALESCE(completed_at,created_at),created_at,id LIMIT 1',
                    (str(root),) + (() if after is None else after))
                if row is None:
                    break
                age = row['created_at'] if row['completed_at'] is None else row['completed_at']
                after = (age,row['created_at'],row['id'])
                if _attempt(row, stop=stop):
                    count += 1
                    free = _free(root)
            log.info('HTML disk cleanup finished: deleted=%s released_bytes=%s free_before=%s free_after=%s',
                     count,max(0,free-before),before,free)
            if free < config.HTML_CLEANUP_TARGET_BYTES and not _cancelled(stop):
                log.warning('HTML cleanup target not reached: free_bytes=%s target_bytes=%s',free,config.HTML_CLEANUP_TARGET_BYTES)
        finally:
            con.close()


class CleanupService:
    def __init__(self):
        self.stop = threading.Event()
        self.wake = asyncio.Event()
        self.initialized = asyncio.Event()
        self.task = None

    async def run(self):
        try:
            while not self.stop.is_set():
                try:
                    await run_blocking(run_cleanup, self.stop, on_cancel=self.stop.set)
                except Exception as exc:
                    log.error('HTML cleanup failed: %s',type(exc).__name__)
                finally:
                    self.initialized.set()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=config.HTML_CLEANUP_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    continue
        finally:
            self.initialized.set()


async def start_cleanup(app):
    service = app.bot_data.get(_APP_KEY)
    if service is None or service.task.done():
        service = CleanupService()
        app.bot_data[_APP_KEY] = service
        # Owned explicitly by post_stop/post_shutdown, not an infinite PTB task
        # (Application.stop() waits for all tasks registered with create_task).
        service.task = asyncio.create_task(service.run(), name='tdb-html-cleanup')
    await service.initialized.wait()


async def stop_cleanup(app):
    service = app.bot_data.get(_APP_KEY)
    if service is not None:
        service.stop.set()
        service.wake.set()
        await service.task
