"""One explicitly selected, fail-closed page repair before append-only storage.

This consumer never assigns a cursor: each validated_pages iterator alone owns
its parent/continuation pair. No persistent ID index or repair storage is used.
"""
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import heapq
import logging
import math
import os
import re
import sys
import time

from src.pagination_diagnostics import CURRENT, _json_chunks
from .client import TwitchAPIError

MAX_REQUESTS = 4
MAX_LOOKAHEAD = 2
CONTROL_SECONDS = 30
MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 4096
MAX_ESTIMATED_BYTES = 32 * 1024 * 1024
PREFIX_PAGES = 2
MAX_START_SHIFT = 2
_STAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.(\d{1,9}))?Z$')
_LOG = logging.getLogger(__name__)


class RepairError(TwitchAPIError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Twitch pagination repair pilot: ' + reason)


def fail(reason):
    raise RepairError(reason)


@dataclass(frozen=True)
class Settings:
    vod: str
    start: Decimal
    end: Decimal
    control_offset: int


def settings(vod):
    if os.environ.get('TCD_PAGINATION_REPAIR_PILOT') != 'true':
        return None
    selected = os.environ.get('TCD_PAGINATION_REPAIR_VOD', '')
    if not re.fullmatch(r'\d{7,12}', selected):
        fail('configuration')
    if str(vod) != selected:
        return None
    values = []
    for name in ('START', 'END', 'CONTROL_OFFSET'):
        value = os.environ.get('TCD_PAGINATION_REPAIR_' + name, '')
        # Offset requests use integers in the existing GraphQL contract. Reject
        # exponent/NaN/inf and truncation rather than silently moving the window.
        if not re.fullmatch(r'\d{1,9}(?:\.\d{1,9})?', value):
            fail('configuration')
        values.append(Decimal(value))
    start, end, control = values
    if not (0 <= control < start <= end) or control != int(control):
        fail('configuration')
    return Settings(selected, start, end, int(control))


def digest(value):
    result = hashlib.sha256()
    for part in _json_chunks(value):
        result.update(part)
    return result.hexdigest()


@dataclass
class Record:
    ident: str
    offset: Decimal
    stamp: str
    clock: tuple
    signature: str
    node: dict
    origin: tuple

    @property
    def key(self):
        return (self.offset, *self.clock)

    @property
    def second(self):
        return int(self.offset)

    @classmethod
    def from_edge(cls, edge, branch, page, position):
        from .fetch_comments import message_row
        node = edge['node']
        ident, offset, stamp = (node.get(k) for k in ('id', 'contentOffsetSeconds', 'createdAt'))
        if not isinstance(ident, str) or not 0 < len(ident) <= 128:
            fail('identity')
        if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
            fail('timestamp')
        match = _STAMP.fullmatch(stamp) if isinstance(stamp, str) and len(stamp) <= 40 else None
        if match is None:
            fail('timestamp')
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            fail('timestamp')
        nanos = int((match[1] or '').ljust(9, '0'))
        clock = (parsed.toordinal(), parsed.hour * 3600 + parsed.minute * 60 + parsed.second, nanos)
        # Covers both the raw identity-bearing content and fallback metadata
        # used by the unchanged renderer (badges/color can live on the node).
        signature = digest({'commenter': node.get('commenter'), 'message': node.get('message'),
                            'row': message_row(node)[2:]})
        return cls(ident, Decimal(str(offset)), stamp, clock, signature, node, (branch, page, position))


def same(left, right):
    if (left.offset, left.stamp, left.signature) != (right.offset, right.stamp, right.signature):
        fail('id_conflict')


def validate_sequence(records):
    last = None
    for item in records:
        if last is not None and item.key < last.key:
            fail('time_regression')
        last = item


def ordered_union(sequences, cancel_check=None):
    """Chronology + source partial order; first-seen tie break, never ID sort."""
    from .fetch_comments import _check_cancel
    records, rank, links, degree = {}, {}, {}, {}
    for sequence in sequences:
        validate_sequence(sequence)
        previous = None
        for item in sequence:
            _check_cancel(cancel_check)
            if item.ident in records:
                same(records[item.ident], item)
            else:
                rank[item.ident] = len(rank)
                records[item.ident] = item
                links[item.ident], degree[item.ident] = set(), 0
            if previous is not None and previous != item.ident and item.ident not in links[previous]:
                links[previous].add(item.ident)
                degree[item.ident] += 1
            previous = item.ident
    ready = [(item.key, rank[ident], ident) for ident, item in records.items() if not degree[ident]]
    heapq.heapify(ready)
    result = []
    while ready:
        _check_cancel(cancel_check)
        _, _, ident = heapq.heappop(ready)
        result.append(records[ident])
        for successor in links[ident]:
            degree[successor] -= 1
            if not degree[successor]:
                heapq.heappush(ready, (records[successor].key, rank[successor], successor))
    if len(result) != len(records):
        fail('order_conflict')
    validate_sequence(result)
    return result


def _memory_size(value, seen):
    ident = id(value)
    if ident in seen:
        return 0
    seen.add(ident)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_memory_size(k, seen) + _memory_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        size += sum(_memory_size(v, seen) for v in value)
    return size


@dataclass
class BufferedPage:
    records: list
    serialized: int
    estimated: int
    has_next: bool


class Retention:
    def __init__(self):
        self.pages = []
        self.peak_rows = self.peak_bytes = self.peak_estimated = 0

    def retain(self, pages):
        self.pages = list(pages)

    def before_request(self):
        # Reserve no unknown-size page in the retained buffer. Incoming JSON is
        # independently bounded at the HTTP reader, then preflighted below.
        if (sum(p.serialized for p in self.pages) >= MAX_BYTES
                or sum(len(p.records) for p in self.pages) >= MAX_RECORDS):
            fail('buffer_limit')

    def accept(self, page, branch):
        if page.next_cursor is not None and len(page.next_cursor) > 4096:
            fail('cursor_limit')
        count = len(page.edges)
        if count + sum(len(p.records) for p in self.pages) > MAX_RECORDS:
            fail('record_limit')
        available = MAX_BYTES - sum(p.serialized for p in self.pages)
        size = 0
        for part in _json_chunks([page.edges, page.next_cursor]):
            size += len(part)
            if size > available:
                fail('buffer_limit')
        # Deliberately conservative allowance for rendered rows, signatures,
        # dictionaries and merge graph; this is an estimate, not an RSS limit.
        estimated = 2 * _memory_size(page.edges, set()) + count * 2048
        if estimated + sum(p.estimated for p in self.pages) > MAX_ESTIMATED_BYTES:
            fail('memory_estimate_limit')
        records = [Record.from_edge(e, branch, page.number, i) for i, e in enumerate(page.edges)]
        validate_sequence(records)
        buffered = BufferedPage(records, size, estimated, page.has_next)
        self.pages.append(buffered)
        self.peak_rows = max(self.peak_rows, sum(len(p.records) for p in self.pages))
        self.peak_bytes = max(self.peak_bytes, sum(p.serialized for p in self.pages))
        self.peak_estimated = max(self.peak_estimated, sum(p.estimated for p in self.pages))
        return buffered


class ControlBudget:
    def __init__(self, retention, cancel_check):
        self.requests = 0
        self.deadline = time.monotonic() + CONTROL_SECONDS
        self.retention, self.cancel_check = retention, cancel_check

    def before_attempt(self):
        from .fetch_comments import _check_cancel
        _check_cancel(self.cancel_check)
        self.retention.before_request()
        if time.monotonic() >= self.deadline:
            fail('control_timeout')
        if self.requests >= MAX_REQUESTS:
            fail('request_limit')
        self.requests += 1


async def repaired_comments(session, client_id, vod_id, start_offset, cancel_check, selected):
    from .fetch_comments import _check_cancel, message_row, validated_pages
    if start_offset != 0:
        fail('main_start_offset')
    journal = CURRENT.get()
    retention = Retention()
    prefix = deque(maxlen=PREFIX_PAGES)
    control_pages, lookahead = [], []
    main = validated_pages(session, client_id, vod_id, 0, cancel_check,
                           before_attempt=retention.before_request, pilot_branch='main', body_limit=MAX_BYTES)
    control = None
    budget = None
    repaired = False
    last = None
    outcome = 'incomplete'

    def event(name, **fields):
        if journal is not None:
            journal.event(name, **fields)

    def emit(item):
        nonlocal last
        _check_cancel(cancel_check)
        if last is not None and item.key < last.key:
            fail('late_time_regression')
        last = item
        row = message_row(item.node)
        if journal is not None:
            journal.repair_emitted(item.origin, bool(row[3]))
        return row

    async def take(iterator, branch):
        retention.before_request()
        _check_cancel(cancel_check)
        try:
            page = await iterator.__anext__()
        except StopAsyncIteration:
            fail('incomplete_coverage')
        return retention.accept(page, branch)

    try:
        event('repair_start', window=1, start=float(selected.start), end=float(selected.end),
              control_offset=selected.control_offset)
        while True:
            retention.retain(prefix)
            try:
                raw = await main.__anext__()
            except StopAsyncIteration:
                if not repaired:
                    fail('window_not_checked')
                outcome = 'success'
                return
            page = retention.accept(raw, 'main')
            if last is not None and page.records[0].key < last.key:
                fail('late_time_regression')
            if repaired or page.records[-1].offset < selected.start:
                recent = {r.ident: r for p in prefix for r in p.records}
                seen = set()
                for item in page.records:
                    if item.ident in recent:
                        same(recent[item.ident], item)
                        fail('duplicate_outside_window')
                    if item.ident in seen:
                        fail('duplicate_outside_window')
                    seen.add(item.ident)
                    row = emit(item)
                    if row[3]:
                        yield row
                prefix.append(page)
                recent.clear()
                seen.clear()
                continue

            low, high = page.records[0].second, page.records[-1].second
            first = page.records[0].offset
            start_shift = max(Decimal(0), first - selected.start)
            if not (first <= selected.end <= page.records[-1].offset
                    and start_shift <= MAX_START_SHIFT):
                fail('window_not_one_page')
            if last is not None and last.second >= low:
                fail('left_boundary_committed')
            prefix_map = {r.ident: r for p in prefix for r in p.records}
            for item in page.records:
                if item.ident in prefix_map:
                    same(prefix_map[item.ident], item)
                    fail('duplicate_outside_window')
            budget = ControlBudget(retention, cancel_check)
            control = validated_pages(session, client_id, vod_id, selected.control_offset, cancel_check,
                                      before_attempt=budget.before_attempt, pilot_branch='control', body_limit=MAX_BYTES)
            # Includes request, retry pauses, pacing, decoding and validation.
            try:
                async with asyncio.timeout(max(0, budget.deadline - time.monotonic())):
                    while True:
                        controlled = await take(control, 'control')
                        if time.monotonic() >= budget.deadline:
                            fail('control_timeout')
                        if not control_pages and controlled.records[0].second >= min(low, int(selected.start)):
                            fail('control_no_left_barrier')
                        if control_pages and controlled.records[0].key < control_pages[-1].records[-1].key:
                            fail('control_time_regression')
                        control_pages.append(controlled)
                        for item in controlled.records:
                            _check_cancel(cancel_check)
                            if item.ident in prefix_map:
                                same(prefix_map[item.ident], item)
                            if item.second < low:
                                known = prefix_map.get(item.ident)
                                if known is None:
                                    fail('unverified_prefix')
                                same(known, item)
                        if controlled.records[-1].second > high:
                            break
                        if not controlled.has_next:
                            fail('control_no_right_barrier')
            except TimeoutError:
                fail('control_timeout')

            branch = [r for p in control_pages for r in p.records if r.second >= low]
            tail = [r for r in branch if r.second > high]
            main_records = list(page.records)
            for _ in range(MAX_LOOKAHEAD):
                continued = await take(main, 'main')  # always the ORIGINAL main iterator
                if continued.records[0].key < main_records[-1].key:
                    fail('main_time_regression')
                lookahead.append(continued)
                for item in continued.records:
                    if item.ident in prefix_map:
                        same(prefix_map[item.ident], item)
                        fail('duplicate_outside_window')
                main_records.extend(continued.records)
                observed = {r.ident: r for r in main_records}
                missing = []
                for item in tail:
                    known = observed.get(item.ident)
                    if known is None:
                        missing.append(item)
                    else:
                        same(known, item)
                if continued.records[-1].second > high and not missing:
                    break
                # Only strict interior absence is conclusive. Uncovered/tied
                # final seconds must wait for the next main page or fail budget.
                if any(item.second < continued.records[-1].second for item in missing):
                    fail('control_tail_missing')
            else:
                fail('main_barrier_or_tail_limit')

            # Include the bounded committed prefix in order/conflict checking,
            # then remove it from output. Its source order cannot be rewritten.
            merged = ordered_union([[r for p in prefix for r in p.records] + main_records,
                                    [r for p in control_pages for r in p.records]], cancel_check)
            repeated_ids = (sum(len(p.records) for p in prefix) + len(main_records)
                            + sum(len(p.records) for p in control_pages) - len(merged))
            merged = [r for r in merged if r.ident not in prefix_map]
            originals = {r.ident for r in main_records if r.second <= high}
            repaired_window = [r for r in merged if r.second <= high]
            added = len({r.ident for r in repaired_window} - originals)
            event('repair_window_verified', window=1, first=low, last=high,
                  configured_start=float(selected.start), start_shift=float(start_shift),
                  original_ids=len(originals), control_ids=len({r.ident for r in branch}),
                  added_ids=added, merged_ids=len(repaired_window),
                  repeated_ids=repeated_ids,
                  conflicts=0, requests=budget.requests, right_barrier=True,
                  tail_ids=len({r.ident for r in tail}), tail_confirmed=True,
                  peak_records=retention.peak_rows, peak_bytes=retention.peak_bytes,
                  peak_estimated_bytes=retention.peak_estimated)
            for item in merged:
                row = emit(item)
                if row[3]:
                    yield row
            repaired = True
            # All consumed lookahead has now been emitted. Continue main after
            # its own last buffered page, not from any control cursor.
            prefix.clear()
            for buffered in [page, *lookahead][-PREFIX_PAGES:]:
                prefix.append(buffered)
            del buffered
            control_pages.clear()
            lookahead.clear()
            main_records.clear()
            branch.clear()
            tail.clear()
            merged.clear()
            prefix_map.clear()
            repaired_window.clear()
            originals.clear()
            observed.clear()
            missing.clear()
            del controlled, continued, raw, page
            await control.aclose()
            control = None
    except RepairError as exc:
        outcome = exc.reason
        _LOG.warning('Pagination repair pilot failed: %s', exc.reason)
        raise
    except (asyncio.CancelledError, GeneratorExit):
        outcome = 'cancelled_or_closed'
        raise
    except BaseException:
        outcome = 'downloader_error'
        raise
    finally:
        try:
            await main.aclose()
        finally:
            try:
                if control is not None:
                    await control.aclose()
            finally:
                prefix.clear()
                control_pages.clear()
                lookahead.clear()
                retention.pages.clear()
                event('repair_end', outcome=outcome, verified=repaired,
                      requests=budget.requests if budget is not None else 0,
                      peak_records=retention.peak_rows, peak_bytes=retention.peak_bytes,
                      peak_estimated_bytes=retention.peak_estimated)
