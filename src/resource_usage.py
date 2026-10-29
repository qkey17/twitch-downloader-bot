"""Lightweight process metrics; all byte values are optional on unsupported OSes."""
import logging
import os
import asyncio
import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from functools import lru_cache


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    return ctypes, Counters, kernel, psapi


def memory_usage() -> dict:
    if os.name == "nt":
        try:
            ctypes, Counters, kernel, psapi = _windows_api()
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return {"rss_bytes": counters.WorkingSetSize, "process_peak_rss_bytes": counters.PeakWorkingSetSize,
                        "swap_bytes": None, "private_bytes": counters.PrivateUsage}
        except (OSError, AttributeError):
            pass
    else:
        try:
            status = Path("/proc/self/status").read_text(encoding="ascii")
            fields = {}
            for line in status.splitlines():
                name, _, value = line.partition(":")
                if name in {"VmRSS", "VmHWM", "VmSwap"}:
                    fields[name] = int(value.split()[0]) * 1024
            return {"rss_bytes": fields.get("VmRSS"), "process_peak_rss_bytes": fields.get("VmHWM"),
                    "swap_bytes": fields.get("VmSwap")}
        except (OSError, ValueError):
            pass
    return {"rss_bytes": None, "process_peak_rss_bytes": None, "swap_bytes": None}


def log_resources(stage, **metrics):
    try:
        operation = _CURRENT.get()
        if operation is not None:
            operation.update(metrics)
            return  # Bounded stage/end events replace per-5000-message logs.
        logging.getLogger("tcd.resources").info(
            "stage=%s memory=%s metrics=%s peak_scope=process_lifetime", stage, memory_usage(), metrics,
        )
    except Exception:
        pass


_CURRENT = ContextVar('resource_operation', default=None)
_METRICS = frozenset(('messages', 'spool_bytes', 'html_bytes'))


def failure_code(exc):
    if isinstance(exc, asyncio.CancelledError) or (isinstance(exc, RuntimeError) and str(exc) == 'Загрузка была отменена.'):
        return 'cancelled'
    if isinstance(exc, RuntimeError) and str(exc) == 'CHAT_EMPTY':
        return 'empty_chat'
    for kind, name in ((TimeoutError, 'timeout'), (OSError, 'io_error'), (ValueError, 'invalid_data')):
        if isinstance(exc, kind):
            return name
    return 'operation_error'


class OperationTelemetry:
    def __init__(self, operation_id):
        self.operation_id = operation_id if isinstance(operation_id, str) and re.fullmatch('[0-9a-f]{32}', operation_id) else None
        self.started = self.clock()
        self.metrics = {}
        self.outcome, self.error = 'success', None
        self.finished = False
        self.begun = False

    def begin(self):
        if not self.begun:
            self.begun = True
            self.emit('operation_start')

    @staticmethod
    def clock():
        try:
            return time.monotonic()
        except Exception:
            return None

    def update(self, values):
        for key in _METRICS:
            value = values.get(key)
            if type(value) is int and value >= 0:
                self.metrics[key] = max(self.metrics.get(key, 0), value)

    def fail(self, exc):
        code = failure_code(exc)
        self.outcome, self.error = ('cancelled' if code == 'cancelled' else 'error'), code

    def emit(self, event, stage=None, started=None, error=None):
        try:
            now = self.clock()
            try:
                memory = memory_usage()
            except Exception:
                memory = {'rss_bytes': None, 'process_peak_rss_bytes': None, 'swap_bytes': None}
            try:
                cpu = time.process_time()
            except Exception:
                cpu = None
            row = dict(event=event, operation_id=self.operation_id, stage=stage,
                       elapsed_seconds=round(max(0, now-started), 6) if now is not None and started is not None else None,
                       process_memory=memory, process_cpu_seconds=cpu,
                       resource_scope='process', peak_scope='process_lifetime',
                       metrics=dict(self.metrics), outcome=self.outcome, error_code=error or self.error)
            logging.getLogger('tcd.resources').info('%s', json.dumps(row, separators=(',', ':')))
        except Exception:
            pass

    def finish(self):
        if not self.finished:
            self.finished = True
            self.emit('operation_end', started=self.started)


def record_failure(exc):
    operation = _CURRENT.get()
    if operation is not None:
        operation.fail(exc)


@contextmanager
def stage(name):
    operation = _CURRENT.get()
    if operation is None:
        yield
        return
    started = operation.clock()
    error = None
    try:
        yield
    except BaseException as exc:
        operation.fail(exc)
        error = failure_code(exc)
        raise
    finally:
        operation.emit('stage_end', stage=name, started=started, error=error)


def measure_stage(name):
    def decorate(function):
        if asyncio.iscoroutinefunction(function):
            @wraps(function)
            async def measured(*args, **kwargs):
                with stage(name):
                    return await function(*args, **kwargs)
        else:
            @wraps(function)
            def measured(*args, **kwargs):
                with stage(name):
                    return function(*args, **kwargs)
        return measured
    return decorate


def observe_operation(function):
    @wraps(function)
    async def observed(*args, **kwargs):
        operation = kwargs.pop('_telemetry', None) or OperationTelemetry(kwargs.get('operation_id', args[7] if len(args) > 7 else None))
        token = _CURRENT.set(operation)
        operation.begin()
        try:
            return await function(*args, **kwargs)
        except BaseException as exc:
            operation.fail(exc)
            raise
        finally:
            operation.finish()
            _CURRENT.reset(token)
    return observed
