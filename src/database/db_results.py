"""Durable HTML generations. No finished files are deleted by this module."""
import hashlib
import json
import logging
import os
import re
import stat
import sqlite3
import time
import uuid
from pathlib import Path

from src import config, html_publisher
from src.file_lock import try_lock_file
from .db_core import _connect
from .db_cache import _upsert_cache

log = logging.getLogger(__name__)
_KEY = re.compile(r"[0-9a-f]{32}")
_NAME = re.compile(r"chat_([0-9a-f]{32})\.html")


def normalize_vod_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("Invalid Twitch VOD ID")
    normalized = value.lstrip("0")
    if not normalized:
        raise ValueError("Invalid Twitch VOD ID")
    return normalized


def create_schema(con):
    statements = (
        """CREATE TABLE IF NOT EXISTS html_results (
            id TEXT PRIMARY KEY NOT NULL, vod_id TEXT NOT NULL, fmt TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin IN ('generated','legacy')),
            state TEXT NOT NULL CHECK(state IN ('building','publishing','ready','failed','cancelled','interrupted','unverified','deleted')),
            root TEXT, filename TEXT, temp_name TEXT, html_ref TEXT, lease_root TEXT,
            managed INTEGER NOT NULL DEFAULT 0 CHECK(managed IN (0,1)),
            size_bytes INTEGER, sha256 TEXT, created_at REAL NOT NULL, prepared_at REAL, completed_at REAL,
            recovered_at REAL, error_code TEXT,
            previous_id TEXT REFERENCES html_results(id) ON DELETE SET NULL,
            cache_id INTEGER REFERENCES vod_cache(id) ON DELETE SET NULL,
            meta_json TEXT NOT NULL DEFAULT '{}', stats_json TEXT NOT NULL DEFAULT '{}',
            files_json TEXT NOT NULL DEFAULT '[]', UNIQUE(id,vod_id,fmt)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_html_results_vod ON html_results(vod_id,fmt,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_html_results_cache ON html_results(cache_id)",
        "CREATE INDEX IF NOT EXISTS idx_html_results_previous ON html_results(previous_id,state)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_html_result_path ON html_results(root,filename) WHERE managed=1",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_html_legacy_cache ON html_results(cache_id) WHERE origin='legacy'",
        """CREATE TABLE IF NOT EXISTS html_deletions (
            result_id TEXT PRIMARY KEY REFERENCES html_results(id),
            phase TEXT NOT NULL CHECK(phase IN ('pending','quarantined','done','cancelled')),
            root TEXT NOT NULL, filename TEXT NOT NULL, quarantine TEXT NOT NULL,
            device TEXT NOT NULL, inode TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            mtime_ns TEXT NOT NULL, dir_device TEXT, dir_inode TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, error_code TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_html_deletions_phase ON html_deletions(phase)",
        """CREATE TABLE IF NOT EXISTS html_removed_heads (
            vod_id TEXT NOT NULL, fmt TEXT NOT NULL, result_id TEXT NOT NULL,
            PRIMARY KEY(vod_id,fmt),
            FOREIGN KEY(result_id,vod_id,fmt) REFERENCES html_results(id,vod_id,fmt)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_html_cleanup_ready ON html_results
           (root,COALESCE(completed_at,created_at),created_at,id) WHERE state='ready' AND managed=1""",
        """CREATE TABLE IF NOT EXISTS html_current (
            vod_id TEXT NOT NULL, fmt TEXT NOT NULL, result_id TEXT NOT NULL,
            PRIMARY KEY(vod_id,fmt),
            FOREIGN KEY(result_id,vod_id,fmt) REFERENCES html_results(id,vod_id,fmt)
        )""",
        """CREATE TRIGGER IF NOT EXISTS html_current_ready_insert BEFORE INSERT ON html_current
           WHEN NOT EXISTS(SELECT 1 FROM html_results WHERE id=NEW.result_id AND state='ready')
           BEGIN SELECT RAISE(ABORT,'HTML generation is not ready'); END""",
        """CREATE TRIGGER IF NOT EXISTS html_current_ready_update BEFORE UPDATE ON html_current
           WHEN NOT EXISTS(SELECT 1 FROM html_results WHERE id=NEW.result_id AND state='ready')
           BEGIN SELECT RAISE(ABORT,'HTML generation is not ready'); END""",
        """CREATE TRIGGER IF NOT EXISTS html_result_current_state BEFORE UPDATE OF state ON html_results
           WHEN NEW.state!='ready' AND EXISTS(SELECT 1 FROM html_current WHERE result_id=OLD.id)
           BEGIN SELECT RAISE(ABORT,'Current HTML generation must remain ready'); END""",
    )
    for statement in statements:
        con.execute(statement)


# Registry-only metadata, carried by both the preparation and the ready row.
_STABLE_SQL = "CASE WHEN json_valid(meta_json) THEN COALESCE(json_type(meta_json, '$._stable_html') = 'true', 0) ELSE 0 END"


def stable_result(row):
    try:
        meta = json.loads(row['meta_json'])
        return isinstance(meta, dict) and meta.get('_stable_html') is True
    except (ValueError, TypeError):
        return False


def _roots():
    return {Path(value).expanduser().resolve() for value in
            (html_publisher.OUT_DIR, html_publisher.PUBLIC_HTML_DIR) if value}


def result_path(row):
    """Validate DB paths against configured roots and the owned filename grammar."""
    root_value, filename = row.get("root"), row.get("filename")
    if not row.get("managed") or not isinstance(root_value,str) or not root_value or not isinstance(filename, str) or not _NAME.fullmatch(filename):
        return None
    root = Path(root_value)
    if not root.is_absolute() or root != root.resolve() or root not in _roots():
        return None
    if row.get("origin") == "generated" and filename != f"chat_{row.get('id')}.html":
        return None
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root:
        return None
    return path


def file_status(row):
    try:
        path = result_path(row)
        if path is None:
            return "unmanaged"
        if not stat.S_ISREG(path.lstat().st_mode):
            return "invalid"
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            if not size or (row.get("size_bytes") is not None and size != row["size_bytes"]):
                return "invalid"
            return "available" if source.read(1) else "invalid"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError, RuntimeError, TypeError):
        return "unavailable"


def _legacy_location(ref):
    try:
        if html_publisher.is_local_html_reference(ref):
            return Path(html_publisher.OUT_DIR).expanduser().resolve(), ref[6:]
        if not isinstance(ref, str) or not html_publisher.PUBLIC_BASE_URL or not html_publisher.PUBLIC_HTML_DIR:
            return None
        name = html_publisher.public_html_filename(ref)
        if name is not None:
            return Path(html_publisher.PUBLIC_HTML_DIR).expanduser().resolve(), name
    except (ValueError, OSError, TypeError, RuntimeError):
        pass
    return None


def _legacy_html_complete(row):
    """Bounded envelope check: do not import obviously truncated legacy files."""
    try:
        path=result_path(row)
        if path is None:
            return False
        with path.open('rb') as source:
            prefix=source.read(4096).lstrip(b'\xef\xbb\xbf \t\r\n').lower()
            source.seek(max(0,path.stat().st_size-4096))
            tail=source.read(4096).rstrip().lower()
        return (prefix.startswith(b'<!doctype html') or prefix.startswith(b'<html')) and tail.endswith(b'</html>')
    except (OSError,ValueError,TypeError,RuntimeError):
        return False


def migrate_legacy(con, vod_id=None):
    query = """SELECT c.* FROM vod_cache c WHERE c.fmt='html_online'
               AND NOT EXISTS(SELECT 1 FROM html_results r WHERE r.cache_id=c.id)"""
    params = ()
    if vod_id is not None:
        query += " AND c.vod_id=?"
        params = (vod_id,)
    for old in con.execute(query, params):
        old = dict(old)
        key = uuid.uuid4().hex
        location = _legacy_location(old.get("html_url"))
        ambiguous = bool(location and con.execute("SELECT 1 FROM html_results WHERE root=? AND filename=? AND managed=1",(str(location[0]),location[1])).fetchone())
        if ambiguous:
            location = None
        row = {"id": key, "origin": "legacy", "managed": int(location is not None),
               "root": str(location[0]) if location else None,
               "filename": location[1] if location else None, "size_bytes": None}
        status = file_status(row)
        if status=='available' and not _legacy_html_complete(row):
            status='invalid_html'
        try:
            size = result_path(row).stat().st_size if status == "available" else None
        except OSError:
            size,status = None,"unavailable"
        try:
            if normalize_vod_id(old['vod_id'])!=old['vod_id']:
                status="invalid_vod_id"
        except ValueError:
            status="invalid_vod_id"
        if ambiguous:
            status="ambiguous_path"
        meta = {name: old.get(name) for name in ("title","channel","created_at","length_seconds","html_url")}
        stats = {name: old.get(name) for name in ("messages","unique_users","parts")}
        # Preserve original cache columns verbatim; omit invalid summary types
        # from JSON rather than letting a legacy BLOB prevent every startup.
        for name in ('title','channel','created_at','html_url'):
            if meta[name] is not None and not isinstance(meta[name],str):
                meta[name]=None
        if meta['length_seconds'] is not None and not isinstance(meta['length_seconds'],(int,float)):
            meta['length_seconds']=None
        for name in stats:
            if stats[name] is not None and not isinstance(stats[name],int):
                stats[name]=None
        state = "ready" if status == "available" else "unverified"
        con.execute("""INSERT INTO html_results(id,vod_id,fmt,origin,state,root,filename,html_ref,managed,
                    size_bytes,created_at,completed_at,cache_id,meta_json,stats_json,error_code)
                    VALUES(?,?,?,'legacy',?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key,old['vod_id'],old['fmt'],state,row['root'],row['filename'],old['html_url'],row['managed'],
                     size,old['cached_at'],old['cached_at'] if state=='ready' else None,old['id'],
                     json.dumps(meta,ensure_ascii=False),json.dumps(stats),None if state=='ready' else status))
        if state == "ready":
            con.execute("""INSERT OR IGNORE INTO html_current(vod_id,fmt,result_id)
                SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM html_removed_heads WHERE vod_id=? AND fmt=?)""",
                (old['vod_id'],old['fmt'],key,old['vod_id'],old['fmt']))
        else:
            log.warning("Legacy HTML cache needs verification: cache_id=%s reason=%s",old['id'],status)
    _retry_legacy(con,vod_id)


def _retry_legacy(con,vod_id):
    query="SELECT * FROM html_results WHERE origin='legacy' AND state='unverified'"
    params=()
    if vod_id is not None:
        query+=' AND vod_id=?'
        params=(vod_id,)
    for stored in con.execute(query,params):
        row=dict(stored)
        try:
            if normalize_vod_id(row['vod_id'])!=row['vod_id']:
                continue
        except ValueError:
            continue
        location=_legacy_location(row['html_ref'])
        if not location:
            continue
        if con.execute('SELECT 1 FROM html_results WHERE root=? AND filename=? AND managed=1 AND id!=?',
                       (str(location[0]),location[1],row['id'])).fetchone():
            continue
        row.update(root=str(location[0]),filename=location[1],managed=1,size_bytes=None)
        if file_status(row)!='available' or not _legacy_html_complete(row):
            continue
        try:
            size=result_path(row).stat().st_size
        except OSError:
            continue
        con.execute("UPDATE html_results SET state='ready',root=?,filename=?,managed=1,size_bytes=?,completed_at=created_at,error_code=NULL WHERE id=?",
                    (row['root'],row['filename'],size,row['id']))
        con.execute('''INSERT OR IGNORE INTO html_current(vod_id,fmt,result_id)
            SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM html_removed_heads WHERE vod_id=? AND fmt=?)''',
                    (row['vod_id'],row['fmt'],row['id'],row['vod_id'],row['fmt']))


def get_ready_result(vod_id, fmt="html_online"):
    vod_id = normalize_vod_id(vod_id)
    con = _connect()
    try:
        row = con.execute("""SELECT r.* FROM html_current h JOIN html_results r ON r.id=h.result_id AND r.vod_id=h.vod_id AND r.fmt=h.fmt
                             WHERE h.vod_id=? AND h.fmt=? AND r.state='ready'
                             AND NOT EXISTS(SELECT 1 FROM html_deletions d WHERE d.result_id=r.id AND d.phase!='cancelled')""",(vod_id,fmt)).fetchone()
        if row is None:
            # Compatibility for callers still populating the old cache API.
            migrate_legacy(con, vod_id)
            con.commit()
            row = con.execute("""SELECT r.* FROM html_current h JOIN html_results r ON r.id=h.result_id AND r.vod_id=h.vod_id AND r.fmt=h.fmt
                                 WHERE h.vod_id=? AND h.fmt=? AND r.state='ready'
                                 AND NOT EXISTS(SELECT 1 FROM html_deletions d WHERE d.result_id=r.id AND d.phase!='cancelled')""",(vod_id,fmt)).fetchone()
        if not row:
            return None
        result = dict(row)
    finally:
        con.close()
    if file_status(result) != "available" or not _reference_matches(result):
        return None  # Do not delete/downgrade rows on transient filesystem errors.
    try:
        result['meta'],result['stats'] = _payload(result)
    except (ValueError,TypeError):
        return None
    return result


def _lease_path(key):
    if not isinstance(key,str) or not _KEY.fullmatch(key):
        raise ValueError("Invalid generation identifier")
    return Path(config.SPOOL_DIR).expanduser().resolve() / f"tdb-result-{key}.lock"


class Publication:
    def __init__(self, vod_id, fmt="html_online", mode=None, *, stable=False):
        self.vod_id = normalize_vod_id(vod_id)
        if fmt != "html_online":
            raise ValueError("Unsupported generation format")
        self.fmt = fmt
        self.mode = mode or config.HTML_PUBLISH_MODE
        if self.mode not in {"local","public"}:
            raise ValueError("Unsupported HTML publication mode")
        if self.mode == "public" and (not html_publisher.PUBLIC_HTML_DIR or not html_publisher.PUBLIC_BASE_URL):
            raise ValueError("Public HTML publication is not configured")
        self.id = uuid.uuid4().hex
        self.root = Path(html_publisher.OUT_DIR if self.mode=='local' else html_publisher.PUBLIC_HTML_DIR).expanduser().resolve()
        self.filename = f"chat_{self.id}.html"
        self.temp_name = f".{self.filename}.{uuid.uuid4().hex}.tmp"
        self.ref = (f"local:{self.filename}" if self.mode=='local' else
                    html_publisher.public_html_url(self.filename))
        self.previous_id = None
        self.meta, self.stats = {}, {}
        self.stable = stable
        self._vod_lease = None
        self._replacement = None
        self._staged_temp = None
        self._published_path = False
        self._committed = False
        self._lease = None
        self._published = False
        self._proof = None
        self._lease_created = False

    def reserve(self):
        if self.stable:
            key = uuid.uuid5(uuid.NAMESPACE_URL, f'tcd:html-online:{self.vod_id}').hex
            self._vod_lease = _recovery_lease(key)
            if self._vod_lease is None:
                raise RuntimeError("HTML publication already in progress for this VOD")
            self._recover_pending()
        self.root.mkdir(parents=True,exist_ok=True)
        if (self.root / self.filename).exists():
            raise FileExistsError("Generation filename already exists")
        lease = _lease_path(self.id)
        lease.parent.mkdir(parents=True,exist_ok=True)
        self._lease = _recovery_lease(self.id, create_only=True)
        self._lease_created = True
        if self._lease is None:
            raise OSError("Could not reserve HTML generation")
        con = _connect()
        try:
            with con:
                # Serialize the previous-head snapshot with deletion claims.
                con.execute("BEGIN IMMEDIATE")
                migrate_legacy(con,self.vod_id)
                previous = con.execute("SELECT result_id FROM html_current WHERE vod_id=? AND fmt=?",(self.vod_id,self.fmt)).fetchone()
                self.previous_id = previous['result_id'] if previous else None
                if self.stable and self.previous_id is not None:
                    head = _replacement_head(con, self.previous_id, self.vod_id, self.fmt)
                    if head and head['root'] == str(self.root) and file_status(head) == 'available' and _reference_matches(head):
                        self._replacement = head
                        self.ref = head['html_ref']
                    elif stable_result(con.execute("SELECT meta_json FROM html_results WHERE id=?", (self.previous_id,)).fetchone()):
                        raise OSError("Stable HTML target is unavailable")
                con.execute("""INSERT INTO html_results(id,vod_id,fmt,origin,state,root,filename,temp_name,
                            html_ref,managed,created_at,previous_id,lease_root,meta_json)
                            VALUES(?,?,?,'generated','building',?,?,?,?,?,?,?,?,?)""",
                            (self.id,self.vod_id,self.fmt,str(self.root),self.filename,self.temp_name,self.ref,
                             int(self._replacement is None),time.time(),self.previous_id,str(lease.parent),
                             '{"_stable_html":true}' if self.stable else '{}'))
        finally:
            con.close()

    def _recover_pending(self):
        con = _connect()
        try:
            keys = con.execute(f"""SELECT id FROM html_results WHERE vod_id=? AND fmt=?
                AND ({_STABLE_SQL}) AND state='publishing'""", (self.vod_id,self.fmt)).fetchall()
            for (key,) in keys:
                _recover_one(con,key)
                pending = con.execute("SELECT state FROM html_results WHERE id=?", (key,)).fetchone()
                if pending and pending[0] == 'publishing':
                    raise OSError("Prior HTML publication still requires recovery")
        finally:
            con.close()

    @property
    def result_id(self):
        return self._replacement['id'] if self._replacement else self.id

    def path(self):
        path = result_path({'id':self.id,'origin':'generated','root':str(self.root),'filename':self.filename,'managed':1})
        if path is None:
            raise ValueError("Unsafe HTML publication path")
        return path

    def stage(self, size_bytes, sha256, *, staged_identity=None):
        meta = dict(self.meta, html_url=self.ref)
        if self.stable:
            meta['_stable_html'] = True
            meta['_staged_html_identity'] = staged_identity
        con = _connect()
        try:
            with con:
                changed = con.execute("""UPDATE html_results SET state='publishing',size_bytes=?,sha256=?,prepared_at=?,
                    meta_json=?,stats_json=? WHERE id=? AND state='building' AND root=? AND filename=?""",
                    (size_bytes,sha256,time.time(),json.dumps(meta,ensure_ascii=False),json.dumps(self.stats),self.id,str(self.root),self.filename)).rowcount
                if changed != 1:
                    raise ValueError("HTML generation no longer owns its reservation")
            self._proof = (size_bytes,sha256)
        finally:
            con.close()

    def complete(self):
        if self.stable:
            return self._complete_stable()
        if not self._published:
            raise ValueError("HTML generation has not been published")
        con = _connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM html_results WHERE id=?",(self.id,)).fetchone()
            if (not row or row['state']!='publishing' or file_status(dict(row))!='available'
                    or (row['size_bytes'],row['sha256']) != self._proof
                    or row['root']!=str(self.root) or row['filename']!=self.filename or row['html_ref']!=self.ref
                    or row['vod_id']!=self.vod_id or row['fmt']!=self.fmt or row['previous_id']!=self.previous_id):
                raise ValueError("HTML generation is not completely published")
            cache_id = _activate(con,dict(row), self.previous_id)
            con.commit()
            return cache_id
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _complete_stable(self):
        if self._staged_temp is None:
            raise ValueError("HTML generation has not been prepared")
        replacement = self._replacement
        lease = _recovery_lease(replacement['id']) if replacement else None
        if replacement and lease is None:
            raise OSError("Current HTML is in use")
        try:
            if replacement:
                target = result_path(replacement)
                if not target:
                    raise OSError("Current HTML target is unsafe")
                old_identity = _file_identity(target.lstat())
                if not _ready_proof(target,replacement):
                    raise OSError("Current HTML does not match its registered proof")
            con = _connect()
            try:
                with con:
                    con.execute("BEGIN IMMEDIATE")
                    row = dict(con.execute("SELECT * FROM html_results WHERE id=?", (self.id,)).fetchone())
                    if row['state'] != 'publishing' or (row['size_bytes'],row['sha256']) != self._proof:
                        raise ValueError("HTML preparation changed before publication")
                    if replacement:
                        if _replacement_head(con, replacement['id'], self.vod_id, self.fmt) != replacement:
                            raise ValueError("Current HTML changed before publication")
                        final = result_path(replacement)
                        if not final or not _reference_matches(replacement) or _file_identity(final.lstat()) != old_identity:
                            raise OSError("Current HTML is unavailable")
                        cache_id = _apply_replacement(con, row, replacement)
                    else:
                        final = self.path()
                        cache_id = _activate(con, row, self.previous_id)
                    # SQLite changes are still private. The committed preparation
                    # record lets recovery finish if replace succeeds before COMMIT.
                    temp, identity = self._staged_temp
                    if _file_identity(temp.lstat()) != identity:
                        raise OSError("Prepared HTML was replaced")
                    if replacement:
                        os.replace(temp, final)
                    else:
                        os.link(temp, final)
                    self._published_path = True
                    html_publisher.sync_html_directory(final.parent)
                self._committed = True
                return cache_id
            finally:
                con.close()
        finally:
            if lease is not None:
                lease.close()

    def fail(self, error):
        if self._published_path:
            return  # Recover using the committed preparation and the visible file.
        con = _connect()
        try:
            with con:
                state = 'cancelled' if type(error).__name__=='CancelledError' or 'Загрузка была отменена.' in str(error) else 'failed'
                con.execute("UPDATE html_results SET state=?,error_code=? WHERE id=? AND state IN ('building','publishing')",
                            (state,type(error).__name__,self.id))
        finally:
            con.close()

    def close(self):
        if self._staged_temp:
            temp, identity = self._staged_temp
            try:
                if _file_identity(temp.lstat()) == identity:
                    temp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("Could not remove owned HTML staging file: id=%s",self.id)
            self._staged_temp = None
        if self._lease is not None:
            self._lease.close()
            self._lease = None
        if self._vod_lease is not None:
            try:
                if self._published_path and not self._committed:
                    self._recover_pending()
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                log.warning("HTML publication recovery deferred: vod=%s",self.vod_id)
            finally:
                self._vod_lease.close()
                self._vod_lease = None
        # Stable pathname: another owner may already hold this inode. Never unlink.
        self._lease_created = False


def _payload(row):
    meta,stats = json.loads(row['meta_json']),json.loads(row['stats_json'])
    if not isinstance(meta,dict) or not isinstance(stats,dict):
        raise ValueError("Invalid generation metadata")
    if any(meta.get(name) is not None and not isinstance(meta[name],str) for name in ('title','channel','created_at')):
        raise ValueError("Invalid generation metadata")
    if any(stats.get(name) is not None and not isinstance(stats[name],int) for name in ('messages','unique_users','parts')):
        raise ValueError("Invalid generation statistics")
    if type(meta.get('length_seconds')) is not int or meta['length_seconds'] < 0:
        meta['length_seconds'] = None  # Optional metadata must not hide a usable HTML.
    meta.pop('_stable_html',None)
    meta.pop('_staged_html_identity',None)
    meta['html_url'] = row['html_ref']
    return meta,stats


def _activate(con,row,expected_previous):
    if normalize_vod_id(row['vod_id'])!=row['vod_id'] or row['fmt']!='html_online' or not _reference_matches(row):
        raise ValueError("Invalid generation identity")
    current = con.execute("SELECT result_id FROM html_current WHERE vod_id=? AND fmt=?",(row['vod_id'],row['fmt'])).fetchone()
    if (current['result_id'] if current else None) != expected_previous:
        # A claim preceding reserve() may finish while this new VOD is built.
        # Only that exact, durably deleted predecessor can satisfy the CAS.
        deleted = con.execute("""SELECT 1 FROM html_results r JOIN html_removed_heads t ON t.result_id=r.id
            WHERE r.id=? AND r.vod_id=? AND r.fmt=? AND r.state='deleted'""",
            (expected_previous,row['vod_id'],row['fmt'])).fetchone()
        if current is not None or not deleted:
            raise ValueError("Current HTML generation changed during processing")
    meta,stats = _payload(row)
    cache_id = _upsert_cache(con,row['vod_id'],row['fmt'],f"https://www.twitch.tv/videos/{row['vod_id']}",meta,stats,[])
    con.execute("UPDATE html_results SET state='ready',completed_at=?,cache_id=? WHERE id=?",(time.time(),cache_id,row['id']))
    con.execute("""INSERT INTO html_current(vod_id,fmt,result_id) VALUES(?,?,?)
                   ON CONFLICT(vod_id,fmt) DO UPDATE SET result_id=excluded.result_id""",(row['vod_id'],row['fmt'],row['id']))
    con.execute('DELETE FROM html_removed_heads WHERE vod_id=? AND fmt=?',(row['vod_id'],row['fmt']))
    return cache_id


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _replacement_head(con, result_id, vod_id, fmt):
    row = con.execute("""SELECT r.* FROM html_current h JOIN html_results r ON r.id=h.result_id
        WHERE h.vod_id=? AND h.fmt=? AND r.id=? AND r.state='ready' AND r.managed=1
        AND NOT EXISTS(SELECT 1 FROM html_deletions d WHERE d.result_id=r.id AND d.phase!='cancelled')""",
        (vod_id, fmt, result_id)).fetchone()
    return dict(row) if row else None


def _apply_replacement(con, pending, head):
    if pending['root'] != head['root'] or pending['html_ref'] != head['html_ref']:
        raise ValueError("Replacement target differs from the reserved current HTML")
    meta, stats = _payload(pending)
    vod_id, fmt = head['vod_id'], head['fmt']
    cache_id = _upsert_cache(con,vod_id,fmt,f"https://www.twitch.tv/videos/{vod_id}",meta,stats,[])
    ready_meta = json.dumps(dict(meta, _stable_html=True), ensure_ascii=False)
    con.execute("""UPDATE html_results SET size_bytes=?,sha256=?,meta_json=?,stats_json=?,
        completed_at=?,cache_id=? WHERE id=?""",
        (pending['size_bytes'],pending['sha256'],ready_meta,pending['stats_json'],time.time(),cache_id,head['id']))
    # The operation journal must not turn into another ready generation.
    con.execute("DELETE FROM html_results WHERE id=?", (pending['id'],))
    return cache_id


def _matches_proof(path, row):
    if path is None or not row['sha256']:
        return False
    digest = hashlib.sha256()
    if not stat.S_ISREG(path.lstat().st_mode):
        return False
    with path.open('rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != row['size_bytes']:
            return False
        for chunk in iter(lambda:source.read(65536), b''):
            digest.update(chunk)
        return digest.hexdigest() == row['sha256'] and _file_identity(info) == _file_identity(path.lstat())


def _ready_proof(path, row):
    return (_matches_proof(path,row) if row['sha256'] else
            row['origin'] == 'legacy' and file_status(row) == 'available' and _legacy_html_complete(row))


def _recover_replacement(con, row):
    lease = _recovery_lease(row['previous_id'])
    if lease is None:
        return
    try:
        _recover_replacement_locked(con,row)
    finally:
        lease.close()


def _recover_replacement_locked(con, row):
    with con:
        con.execute("BEGIN IMMEDIATE")
        head = _replacement_head(con,row['previous_id'],row['vod_id'],row['fmt'])
        if head is None:
            # No writer may silently install a second name over ambiguous state.
            return
        if row['root'] != head['root'] or row['html_ref'] != head['html_ref'] or not _reference_matches(head):
            return
        path = result_path(head)
        if path is None:
            return
        identity = json.loads(row['meta_json']).get('_staged_html_identity')
        if identity == list(_file_identity(path.lstat())) and _matches_proof(path,row):
            html_publisher.sync_html_directory(path.parent)
            _apply_replacement(con,row,head)
        elif _ready_proof(path,head):
            con.execute("UPDATE html_results SET state='interrupted',recovered_at=? WHERE id=?",
                        (time.time(),row['id']))


def _reference_matches(row):
    location = _legacy_location(row.get('html_ref'))
    try:
        path = result_path(row)
        return bool(location and path and location == (path.parent,path.name))
    except (OSError,ValueError,TypeError,RuntimeError):
        return False


def _recovery_lease(key, *, create_only=False):
    """Acquire a stable generation lock. Closing releases ownership, not its name.

    Even orphan/deleted-generation markers are retained: deleting after unlock
    can split a concurrent owner's lock from a newly created inode.
    """
    path = _lease_path(key)
    path.parent.mkdir(parents=True,exist_ok=True)
    flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        if create_only:
            raise
        if path.is_symlink():
            return None
        fd = os.open(path, flags)
        created = False
    handle = os.fdopen(fd, 'r+b')
    try:
        info, named = os.fstat(handle.fileno()), path.lstat()
        if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(named.st_mode)
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            handle.close()
            return None
        if not try_lock_file(handle):
            handle.close()
            return None
        marker = f"TDB_RESULT_V1 {key}\n".encode('ascii')
        if created:
            handle.write(marker)
            handle.flush()
        elif handle.read(len(marker)+1) != marker:
            handle.close()
            log.warning("Unrecognized generation lease retained: id=%s",key)
            return None
        return handle
    except BaseException:
        handle.close()
        raise


def _recover_one(con,key):
    stored = con.execute("SELECT lease_root FROM html_results WHERE id=?", (key,)).fetchone()
    if stored is None or stored['lease_root'] != str(_lease_path(key).parent):
        return
    lease = _recovery_lease(key)
    if lease is None:
        return
    try:
        stored = con.execute("SELECT * FROM html_results WHERE id=?", (key,)).fetchone()
        if not stored or stored['state'] not in ('building','publishing','failed','cancelled','interrupted') or stored['recovered_at'] is not None:
            return
        row = dict(stored)
        if stable_result(row) and not row['managed']:
            if row['state'] == 'publishing':
                _recover_replacement(con,row)
            else:
                with con:
                    con.execute("""UPDATE html_results SET state=CASE WHEN state='building' THEN 'interrupted' ELSE state END,
                                recovered_at=? WHERE id=?""", (time.time(),key))
            return
        status = file_status(row)
        if status == 'unavailable':
            log.warning("Generation recovery deferred: id=%s",key)
            return
        valid = False
        if status=='available' and row['sha256'] and row['error_code']!='FileExistsError' and _reference_matches(row):
            path = result_path(row)
            valid = _matches_proof(path,row)
            meta,stats = json.loads(row['meta_json']),json.loads(row['stats_json'])
            valid = valid and isinstance(meta,dict) and isinstance(stats,dict)
            if valid and stable_result(row):
                valid = meta.get('_staged_html_identity') == list(_file_identity(path.lstat()))
        # A reservation proves only intent, not creation of a particular file.
        # Even matching bytes/inodes cannot prove ownership after a crash.
        path=result_path(row)
        temp=row['temp_name']
        if path and temp and re.fullmatch(rf"\.chat_{re.escape(key)}\.html\.[0-9a-f]{{32}}\.tmp",temp):
            candidate=path.parent/temp
            if candidate.exists() or candidate.is_symlink():
                log.warning("Registered HTML temp retained; ownership unproven: id=%s",key)
        elif temp:
            log.warning("Unrecognized generation temporary path retained: id=%s",key)
        with con:
            if valid:
                # Never displace an older ready head during recovery. A first
                # publication interrupted after linking may be adopted safely.
                if row['state']=='publishing' and stable_result(row):
                    html_publisher.sync_html_directory(path.parent)
                    _activate(con,row,row['previous_id'])
                elif row['state']=='publishing' and row['previous_id'] is None:
                    head=con.execute("SELECT 1 FROM html_current WHERE vod_id=? AND fmt=?",(row['vod_id'],row['fmt'])).fetchone()
                    if not head:
                        _activate(con,row,None)
                con.execute("UPDATE html_results SET state='ready',completed_at=COALESCE(completed_at,?),recovered_at=? WHERE id=?",
                            (row['prepared_at'] or row['created_at'],time.time(),key))
            else:
                con.execute("UPDATE html_results SET state=CASE WHEN state IN ('building','publishing') THEN 'interrupted' ELSE state END,recovered_at=? WHERE id=?",
                            (time.time(),key))
                if status!='missing':
                    log.warning("Unverified HTML artifact retained: id=%s status=%s",key,status)
    finally:
        lease.close()


def recover_results():
    """Idempotent, streaming recovery; never enumerate/delete finished HTML."""
    con = _connect()
    try:
        ids = con.execute("SELECT id FROM html_results WHERE origin='generated' AND state IN ('building','publishing','failed','cancelled','interrupted') AND recovered_at IS NULL")
        for (key,) in ids:
            if not isinstance(key,str) or not _KEY.fullmatch(key):
                log.warning("Invalid generation identifier; recovery skipped")
                continue
            try:
                _recover_one(con,key)
            except (OSError,ValueError,TypeError,RuntimeError,sqlite3.Error) as exc:
                log.warning("Generation recovery deferred: id=%s error=%s",key,type(exc).__name__)
        # A crash before the INSERT can leave only a lease, not a finished HTML.
        root=Path(config.SPOOL_DIR).expanduser().resolve()
        if root.exists():
            for path in root.iterdir():
                match=re.fullmatch(r"tdb-result-([0-9a-f]{32})\.lock",path.name)
                if not match or path.is_symlink() or not path.is_file():
                    continue
                key=match.group(1)
                if con.execute("SELECT 1 FROM html_results WHERE id=?",(key,)).fetchone():
                    continue
                lease=_recovery_lease(key)
                if lease is not None:
                    lease.close()
    finally:
        con.close()


def protected_temp_files():
    """Keep planned generation temps out of age cleanup, regardless of state."""
    con=_connect()
    protected=set()
    try:
        for record in con.execute(f"""SELECT id,root,temp_name FROM html_results WHERE origin='generated'
            AND (NOT ({_STABLE_SQL}) OR state IN ('building','publishing'))"""):
            key,root,temp=record
            if (isinstance(key,str) and _KEY.fullmatch(key) and isinstance(root,str)
                    and isinstance(temp,str) and html_publisher._TEMP_HTML_FILENAME_RE.fullmatch(temp)):
                directory=Path(root)
                try:
                    if directory.is_absolute() and directory==directory.resolve() and directory in _roots():
                        protected.add(directory/temp)
                except (OSError,ValueError,RuntimeError):
                    log.warning('Invalid temporary collision path retained: id=%s',key)
        return protected
    finally:
        con.close()
