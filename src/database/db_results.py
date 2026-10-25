"""Durable HTML generations. No finished files are deleted by this module."""
import hashlib
import json
import logging
import re
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
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_html_result_path ON html_results(root,filename) WHERE managed=1",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_html_legacy_cache ON html_results(cache_id) WHERE origin='legacy'",
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
        with path.open("rb") as source:
            size = path.stat().st_size
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
            con.execute("INSERT OR IGNORE INTO html_current(vod_id,fmt,result_id) VALUES(?,?,?)",(old['vod_id'],old['fmt'],key))
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
        con.execute('INSERT OR IGNORE INTO html_current(vod_id,fmt,result_id) VALUES(?,?,?)',
                    (row['vod_id'],row['fmt'],row['id']))


def get_ready_result(vod_id, fmt="html_online"):
    vod_id = normalize_vod_id(vod_id)
    con = _connect()
    try:
        row = con.execute("""SELECT r.* FROM html_current h JOIN html_results r ON r.id=h.result_id AND r.vod_id=h.vod_id AND r.fmt=h.fmt
                             WHERE h.vod_id=? AND h.fmt=? AND r.state='ready'""",(vod_id,fmt)).fetchone()
        if row is None:
            # Compatibility for callers still populating the old cache API.
            migrate_legacy(con, vod_id)
            con.commit()
            row = con.execute("""SELECT r.* FROM html_current h JOIN html_results r ON r.id=h.result_id AND r.vod_id=h.vod_id AND r.fmt=h.fmt
                                 WHERE h.vod_id=? AND h.fmt=? AND r.state='ready'""",(vod_id,fmt)).fetchone()
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
    def __init__(self, vod_id, fmt="html_online", mode=None):
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
        self._lease = None
        self._published = False
        self._proof = None
        self._lease_created = False

    def reserve(self):
        self.root.mkdir(parents=True,exist_ok=True)
        if (self.root / self.filename).exists():
            raise FileExistsError("Generation filename already exists")
        lease = _lease_path(self.id)
        lease.parent.mkdir(parents=True,exist_ok=True)
        self._lease = lease.open("x+b")
        self._lease_created = True
        if not try_lock_file(self._lease):
            raise OSError("Could not reserve HTML generation")
        self._lease.write(f"TDB_RESULT_V1 {self.id}\n".encode('ascii'))
        self._lease.flush()
        con = _connect()
        try:
            with con:
                migrate_legacy(con,self.vod_id)
                previous = con.execute("SELECT result_id FROM html_current WHERE vod_id=? AND fmt=?",(self.vod_id,self.fmt)).fetchone()
                self.previous_id = previous['result_id'] if previous else None
                con.execute("""INSERT INTO html_results(id,vod_id,fmt,origin,state,root,filename,temp_name,
                            html_ref,managed,created_at,previous_id,lease_root) VALUES(?,?,?,'generated','building',?,?,?,?,1,?,?,?)""",
                            (self.id,self.vod_id,self.fmt,str(self.root),self.filename,self.temp_name,self.ref,time.time(),self.previous_id,str(lease.parent)))
        finally:
            con.close()

    def path(self):
        path = result_path({'id':self.id,'origin':'generated','root':str(self.root),'filename':self.filename,'managed':1})
        if path is None:
            raise ValueError("Unsafe HTML publication path")
        return path

    def stage(self, size_bytes, sha256):
        meta = dict(self.meta, html_url=self.ref)
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

    def fail(self, error):
        con = _connect()
        try:
            with con:
                state = 'cancelled' if type(error).__name__=='CancelledError' or 'Загрузка была отменена.' in str(error) else 'failed'
                con.execute("UPDATE html_results SET state=?,error_code=? WHERE id=? AND state IN ('building','publishing')",
                            (state,type(error).__name__,self.id))
        finally:
            con.close()

    def close(self):
        if self._lease is not None:
            self._lease.close()
            self._lease = None
        if self._lease_created:
            _lease_path(self.id).unlink(missing_ok=True)
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
    meta['html_url'] = row['html_ref']
    return meta,stats


def _activate(con,row,expected_previous):
    if normalize_vod_id(row['vod_id'])!=row['vod_id'] or row['fmt']!='html_online' or not _reference_matches(row):
        raise ValueError("Invalid generation identity")
    current = con.execute("SELECT result_id FROM html_current WHERE vod_id=? AND fmt=?",(row['vod_id'],row['fmt'])).fetchone()
    if (current['result_id'] if current else None) != expected_previous:
        raise ValueError("Current HTML generation changed during processing")
    meta,stats = _payload(row)
    cache_id = _upsert_cache(con,row['vod_id'],row['fmt'],f"https://www.twitch.tv/videos/{row['vod_id']}",meta,stats,[])
    con.execute("UPDATE html_results SET state='ready',completed_at=?,cache_id=? WHERE id=?",(time.time(),cache_id,row['id']))
    con.execute("""INSERT INTO html_current(vod_id,fmt,result_id) VALUES(?,?,?)
                   ON CONFLICT(vod_id,fmt) DO UPDATE SET result_id=excluded.result_id""",(row['vod_id'],row['fmt'],row['id']))
    return cache_id


def _reference_matches(row):
    location = _legacy_location(row.get('html_ref'))
    try:
        path = result_path(row)
        return bool(location and path and location == (path.parent,path.name))
    except (OSError,ValueError,TypeError,RuntimeError):
        return False


def _recovery_lease(key):
    path = _lease_path(key)
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        handle = path.open('x+b')
        created = True
    except FileExistsError:
        handle = path.open('r+b')
        created = False
    try:
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
    row = dict(con.execute("SELECT * FROM html_results WHERE id=?",(key,)).fetchone())
    if row['lease_root'] != str(_lease_path(key).parent):
        log.warning("Generation work root changed; recovery skipped: id=%s",key)
        return
    lease = _recovery_lease(key)
    if lease is None:
        return
    try:
        status = file_status(row)
        if status == 'unavailable':
            log.warning("Generation recovery deferred: id=%s",key)
            return
        valid = False
        if status=='available' and row['sha256'] and row['error_code']!='FileExistsError' and _reference_matches(row):
            digest=hashlib.sha256()
            with result_path(row).open('rb') as source:
                for chunk in iter(lambda:source.read(65536),b''):
                    digest.update(chunk)
            valid = digest.hexdigest()==row['sha256']
            if not isinstance(json.loads(row['meta_json']),dict) or not isinstance(json.loads(row['stats_json']),dict):
                valid=False
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
                if row['state']=='publishing' and row['previous_id'] is None:
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
    _lease_path(key).unlink(missing_ok=True)


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
            except (OSError,ValueError,TypeError,RuntimeError) as exc:
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
                    path.unlink(missing_ok=True)
    finally:
        con.close()


def protected_temp_files():
    """Keep planned generation temps out of age cleanup, regardless of state."""
    con=_connect()
    protected=set()
    try:
        for record in con.execute("SELECT id,root,temp_name FROM html_results WHERE origin='generated'"):
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
