"""Opt-in, bounded private pagination evidence. Never drives the downloader."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from src import config
from src.file_lock import try_lock_file

CURRENT = ContextVar('pagination_diagnostic', default=None)
MAX_BYTES = 8 * 1024 * 1024
SLOTS = 4
TTL_SECONDS = 24 * 3600
RESERVE = 4096
SCHEMA = 'tcd-pagination-v1'
_UUID = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
_STAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z$')
_LOG = logging.getLogger(__name__)


def _warn(message):
    try:
        _LOG.warning(message)
    except Exception:
        pass


def _json_chunks(value):
    """Canonical JSON: sorted object keys, ASCII escapes, no spaces or NaN.

    Splitting strings before escaping bounds temporary encoding memory.
    """
    if isinstance(value, str):
        yield b'"'
        for start in range(0, len(value), 4096):
            yield json.dumps(value[start:start + 4096], ensure_ascii=True)[1:-1].encode('ascii')
        yield b'"'
    elif isinstance(value, (list, tuple)):
        yield b'['
        for index, item in enumerate(value):
            if index:
                yield b','
            yield from _json_chunks(item)
        yield b']'
    elif isinstance(value, dict):
        yield b'{'
        for index, key in enumerate(sorted(value)):
            if index:
                yield b','
            yield from _json_chunks(key)
            yield b':'
            yield from _json_chunks(value[key])
        yield b'}'
    else:
        yield json.dumps(value, allow_nan=False, separators=(',', ':')).encode('ascii')


def content_hash(node):
    digest = hashlib.sha256()
    for chunk in _json_chunks({'commenter': node.get('commenter'), 'message': node.get('message')}):
        digest.update(chunk)
    return digest.hexdigest()


def ids_hash(edges):
    """SHA-256 of canonical JSON array of ALL node.id values, in edge order."""
    digest = hashlib.sha256(b'[')
    for index, edge in enumerate(edges):
        if index:
            digest.update(b',')
        node = edge.get('node') if isinstance(edge, dict) else None
        ident = node.get('id') if isinstance(node, dict) else None
        for chunk in _json_chunks(ident):
            digest.update(chunk)
    digest.update(b']')
    return digest.hexdigest()


def _cursor_hash(value):
    if not isinstance(value, str):
        return None
    digest = hashlib.sha256()
    for start in range(0, len(value), 4096):
        digest.update(value[start:start + 4096].encode('utf-8', errors='surrogatepass'))
    return digest.hexdigest()


def _id(value):
    # Fail closed on unexpected API fields; never persist arbitrary echoed text.
    return value if isinstance(value, str) and _UUID.fullmatch(value) else None


def _offset(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _stamp(value):
    return value if isinstance(value, str) and _STAMP.fullmatch(value) else None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Settings:
    vod: str
    start: float
    end: float
    directory: Path


def settings(vod):
    if os.getenv('TCD_PAGINATION_DIAGNOSTICS', 'false') != 'true':
        return None
    chosen = os.getenv('TCD_PAGINATION_DIAG_VOD', '')
    if not re.fullmatch(r'[0-9]{7,12}', chosen) or chosen != vod:
        return None
    try:
        start = float(os.environ['TCD_PAGINATION_DIAG_START'])
        end = float(os.environ['TCD_PAGINATION_DIAG_END'])
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
            raise ValueError
        if os.name != 'posix':
            raise ValueError  # No claim of private Unix permissions on other OSes.
        directory = Path(os.getenv('TCD_PAGINATION_DIAG_DIR') or f'/tmp/tdb-pagination-{os.getuid()}')
        if directory.parent != Path('/tmp') or not directory.name.startswith(f'tdb-pagination-{os.getuid()}'):
            raise ValueError
        for forbidden in (config.PROJECT_ROOT, config.OUT_DIR, config.SPOOL_DIR,
                          Path(config.DB_PATH).absolute().parent, config.PUBLIC_HTML_DIR):
            if forbidden and directory.is_relative_to(Path(forbidden).resolve()):
                raise ValueError
        return Settings(chosen, start, end, directory)
    except (KeyError, ValueError, OSError):
        _warn('Pagination diagnostics disabled: invalid or unsupported private configuration')
        return None


def _private(info, directory=False):
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or info.st_mode & 0o077 or (not directory and info.st_nlink != 1):
        raise OSError('unsafe diagnostic storage')


class Journal:
    """Four fixed 8-MiB slots; one non-blocking OS lease for the writer lifetime.

    Competing diagnostic operations are skipped, never downloads. All relative
    filesystem operations use an open, verified directory descriptor.
    """
    def __init__(self, selected):
        self.selected = selected
        self.root = self.gate = self.file = None
        self.slot = None
        self.size = 0
        self.stopped = False
        self.operation = uuid.uuid4().hex
        self.emitted = 0
        self.page_emitted = self.page_discarded = 0
        self.page_number = self.attempt = None
        self.detail = False
        self.token = None

    def open(self):
        try:
            # /tmp must itself be the real, non-symlink parent.
            if Path('/tmp').resolve() != Path('/tmp'):
                raise OSError('unsafe temporary directory')
            try:
                self.selected.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self.root = os.open(self.selected.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _private(os.fstat(self.root), directory=True)
            self.gate = os.open('gate.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.root)
            _private(os.fstat(self.gate))
            if os.fstat(self.gate).st_size != 0 or not try_lock_file(self.gate):
                raise OSError('diagnostic lease unavailable')
            available = []
            for slot in range(SLOTS):
                present = []
                for suffix in ('active', 'ndjson'):
                    name = f'slot-{slot}.{suffix}'
                    try:
                        info = os.stat(name, dir_fd=self.root, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    _private(info)
                    if info.st_size > MAX_BYTES:
                        raise OSError('oversized diagnostic slot')
                    if suffix == 'ndjson' and time.time() - info.st_mtime >= TTL_SECONDS and self._completed(name):
                        os.unlink(name, dir_fd=self.root)
                    else:
                        present.append(name)
                if len(present) > 1:
                    raise OSError('ambiguous diagnostic slot')
                if not present:
                    available.append(slot)
            if not available:
                raise OSError('diagnostic slots full')
            self.slot = available[0]
            self.file = os.open(f'slot-{self.slot}.active', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=self.root)
            _private(os.fstat(self.file))
            source = hashlib.sha256()
            for name in ('pagination_diagnostics.py', 'twitch_api/client.py', 'twitch_api/fetch_page.py',
                         'twitch_api/fetch_comments.py', 'twitch_api/meta.py', 'workers/download_worker.py'):
                with (Path(__file__).parent / name).open('rb') as handle:
                    for chunk in iter(lambda: handle.read(65536), b''):
                        source.update(chunk)
            self.event('operation_start', source_sha256=source.hexdigest(),
                       graphql_operation='VideoCommentsByOffsetOrCursor', query_sha256=config.VIDEO_COMMENTS_HASH,
                       window_start=self.selected.start, window_end=self.selected.end)
            return not self.stopped
        except Exception:
            self._failure()
            return False

    def _completed(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.root)
        try:
            _private(os.fstat(fd))
            first = os.read(fd, 4096).split(b'\n')[0]
            os.lseek(fd, max(0, os.fstat(fd).st_size - RESERVE), os.SEEK_SET)
            lines = os.read(fd, RESERVE).splitlines()
            head, tail = json.loads(first), json.loads(lines[-1])
            return (head.get('schema') == SCHEMA and head.get('event') == 'operation_start'
                    and re.fullmatch('[0-9a-f]{32}', head.get('diagnostic_operation', ''))
                    and tail.get('schema') == SCHEMA and tail.get('event') in ('operation_end', 'truncated')
                    and head.get('diagnostic_operation') == tail.get('diagnostic_operation'))
        except (ValueError, IndexError):
            return False
        finally:
            os.close(fd)

    def _write(self, record):
        raw = (json.dumps(record, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')
        if self.size + len(raw) > MAX_BYTES:
            raise OSError('diagnostic byte bound')
        remaining = memoryview(raw)
        while remaining:
            count = os.write(self.file, remaining)
            if count <= 0:
                raise OSError('diagnostic short write')
            self.size += count
            remaining = remaining[count:]

    def event(self, event, **fields):
        if self.stopped:
            return
        try:
            record = dict(schema=SCHEMA, diagnostic_operation=self.operation, vod_id=self.selected.vod,
                          utc=utc_now(), event=event, **fields)
            # All records consist only of fixed keys and bounded technical fields.
            encoded_size = len(json.dumps(record, ensure_ascii=True, separators=(',', ':'))) + 1
            if self.size + encoded_size > MAX_BYTES - RESERVE:
                self._write(dict(schema=SCHEMA, diagnostic_operation=self.operation, vod_id=self.selected.vod,
                                 utc=utc_now(), event='truncated', reason='byte_limit', incomplete=True))
                self._finish_file()
                return
            self._write(record)
        except Exception:
            self._failure()

    def _finish_file(self):
        if self.file is not None:
            os.close(self.file)
            self.file = None
            os.rename(f'slot-{self.slot}.active', f'slot-{self.slot}.ndjson',
                      src_dir_fd=self.root, dst_dir_fd=self.root)
        self._release()

    def _release(self):
        self.stopped = True
        for name in ('file', 'gate', 'root'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _failure(self):
        self._release()
        # No exception text, filenames, IDs, headers, response bodies or secrets.
        _warn('Pagination diagnostics disabled for this operation: storage or recording failure')

    def request(self, page, attempt, cursor, offset):
        self.page_number, self.attempt = page, attempt
        self.page_emitted = self.page_discarded = 0
        try:
            self.event('request', page=page, attempt=attempt, request_type='cursor' if cursor else 'offset',
                       input_cursor_sha256=_cursor_hash(cursor), start_offset=None if cursor else _offset(offset))
        except Exception:
            self._failure()

    def response(self, edges, cursor, has_next):
        if self.stopped:
            return
        try:
            low = high = None
            for edge in edges:
                offset = _offset(edge['node'].get('contentOffsetSeconds'))
                if offset is not None:
                    low = offset if low is None else min(low, offset)
                    high = offset if high is None else max(high, offset)
            intersects = bool(low is not None and low <= self.selected.end and high >= self.selected.start)
            self.detail = intersects
            self.event('page_received', page=self.page_number, attempt=self.attempt,
                       output_cursor_sha256=_cursor_hash(cursor), has_next=has_next, edges=len(edges),
                       valid=len(edges), discarded=None, emitted=None, spooled=None,
                       first_offset=_offset(edges[0]['node'].get('contentOffsetSeconds')) if edges else None,
                       last_offset=_offset(edges[-1]['node'].get('contentOffsetSeconds')) if edges else None,
                       first_id=_id(edges[0]['node'].get('id')) if edges else None,
                       last_id=_id(edges[-1]['node'].get('id')) if edges else None,
                       ids_sha256=ids_hash(edges), ids_present=all(isinstance(e['node'].get('id'), str) for e in edges),
                       identity_complete=all(_id(e['node'].get('id')) is not None
                                             and _offset(e['node'].get('contentOffsetSeconds')) is not None
                                             and _stamp(e['node'].get('createdAt')) is not None for e in edges),
                       detail_selected=intersects)
            if intersects:
                for position, edge in enumerate(edges):
                    if self.stopped:
                        break
                    node = edge['node']
                    self.event('message_received', page=self.page_number, attempt=self.attempt, position=position,
                               id=_id(node.get('id')), offset=_offset(node.get('contentOffsetSeconds')),
                               created_at=_stamp(node.get('createdAt')), content_sha256=content_hash(node))
                self.event('detail_end', page=self.page_number, attempt=self.attempt, entries=len(edges), complete=True)
        except Exception:
            self._failure()

    def emitted_row(self, position, nonblank):
        if self.stopped:
            return
        if nonblank:
            self.emitted += 1
            self.page_emitted += 1
        else:
            self.page_discarded += 1
        if self.detail:
            self.event('row_processed', page=self.page_number, attempt=self.attempt, position=position,
                       emitted_sequence=self.emitted if nonblank else None, emitted=nonblank)

    def page_end(self):
        self.event('page_emitted', page=self.page_number, attempt=self.attempt,
                   emitted=self.page_emitted, discarded=self.page_discarded, emitted_total=self.emitted,
                   spooled=None)

    def error(self, phase, exc):
        # Exception classes only; do not serialize arbitrary exception messages.
        try:
            name = type(exc).__name__
            category = name if name in ('TwitchAPIError', 'TimeoutError', 'CancelledError', 'GeneratorExit') else 'other'
            reasons = {'Twitch returned invalid JSON': 'invalid_json',
                       'Twitch GraphQL returned errors': 'graphql_errors',
                       'Twitch network request failed after retries': 'network_retries_exhausted',
                       'Twitch comments pagination made no progress': 'cursor_cycle'}
            message = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ''
            status = getattr(exc, 'status', None)
            retryable = getattr(exc, 'retryable', None)
            self.event('error', page=self.page_number, attempt=self.attempt, phase=phase, category=category,
                       reason=reasons.get(message, 'request_or_local_failure'),
                       http_status=status if type(status) is int else None,
                       retryable=retryable if type(retryable) is bool else None)
        except Exception:
            self._failure()

    def close(self, outcome, spooled):
        try:
            if not self.stopped:
                self.event('operation_end', outcome=outcome, emitted_total=self.emitted, spooled_total=spooled,
                           incomplete=outcome != 'success')
                if not self.stopped:
                    self._finish_file()
        except Exception:
            self._failure()
        finally:
            if self.token is not None:
                try:
                    CURRENT.reset(self.token)
                except Exception:
                    _warn('Pagination diagnostic context could not be restored')
                self.token = None


def begin(vod):
    try:
        selected = settings(vod)
        if selected is None:
            return None
        journal = Journal(selected)
        if not journal.open():
            return None
        journal.token = CURRENT.set(journal)
        return journal
    except Exception:
        _warn('Pagination diagnostics disabled: initialization failure')
        return None
