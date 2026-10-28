"""Scenario-09-only observation of unmodified controller calls (Python 3.12+).

No line tracing, response substitution, tokens, config dumps or per-probe file IO.
Only selected code objects have local PY_RETURN events. Unwind locals are read
from the real exception traceback; post-stop processes remain journal-only.
"""
import functools
import re
import sys
import time
from pathlib import Path
from types import CodeType

FIELDS = ('MainPID', 'InvocationID', 'NRestarts', 'Job', 'LoadState', 'ActiveState', 'SubState')
LOCALS = ('deadline', 'ready_deadline', 'stable_since', 'now', 'ready_process', 'stable_process', 'process')


def state(data):
    return {k: data[k] for k in FIELDS if k in data}


def cpu():
    try:
        group = next(l.split(':', 2)[2] for l in Path('/proc/self/cgroup').read_text().splitlines() if l.startswith('0::'))
        if '..' in Path(group).parts:
            return None
        content = (Path('/sys/fs/cgroup') / group.lstrip('/') / 'cpu.stat').read_text()[:2048]
        allowed = {'usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec'}
        return {k: int(v) for k, v in (line.split() for line in content.splitlines()) if k in allowed}
    except (OSError, ValueError, StopIteration):
        return None


class Trace:
    def __init__(self, module, write, limit=400):
        self.module, self.write, self.limit = module, write, limit
        self.records, self.dropped, self.sequence = [], 0, 0
        self.outcomes = []
        self.recovery = 0
        self.patches, self.codes, self.tool = [], [], None
        self.observation_errors = 0

    def emit(self, kind, **fields):
        try:
            self.sequence += 1
            record = dict(kind=kind, mono=time.monotonic(), recovery=self.recovery,
                          sequence=self.sequence, **fields)
            if kind in ('recovery_error', 'recovery_end'):
                self.outcomes.append(record)
                self.outcomes = self.outcomes[-16:]
            if len(self.records) >= self.limit:
                self.dropped += 1
                return
            self.records.append(record)
        except Exception:
            self.observation_errors += 1

    def locals(self, frame):
        values = frame.f_locals
        result = {k: values[k] for k in LOCALS if k in values and
                  (values[k] is None or isinstance(values[k], (int, float, tuple)))}
        return result

    def returned(self, code, offset, value):
        try:
            frame = sys._getframe(1)
            if frame.f_code is not code:
                self.emit('frame_unavailable', method=code.co_name)
                return
            data = dict(method=code.co_name, offset=offset, line=frame.f_lineno,
                        direct_locals=self.locals(frame))
            if code.co_name == '_process_identity':
                data.update(input=state(frame.f_locals.get('data', {})), identity=value)
            elif code.co_name == '_ready_process':
                data['identity'] = value
                reply = frame.f_locals.get('reply')
                if isinstance(reply, dict):
                    data['reply'] = {k: reply[k] for k in ('pid','status','running','committed') if k in reply}
                    data['reply']['token_present'] = reply.get('token') is not None
            self.emit('function_return', **data)
        except Exception:
            self.observation_errors += 1

    def patch(self, cls, name, wrapper):
        original = getattr(cls, name)
        self.patches.append((cls, name, original))
        setattr(cls, name, wrapper(original))

    def observed(self, kind, details):
        def decorate(original):
            @functools.wraps(original)
            def call(*args, **kwargs):
                try:
                    self.emit(kind+'_begin', **details(args, kwargs))
                except Exception:
                    self.observation_errors += 1
                try:
                    value = original(*args, **kwargs)
                except BaseException as error:
                    self.emit(kind+'_error', error_type=type(error).__name__)
                    raise
                try:
                    self.emit(kind+'_end', result=state(value) if kind == 'systemd' else
                              {k: value[k] for k in ('pid','status','running','committed') if k in value},
                              **({'token_present': value.get('token') is not None} if kind == 'socket' else {}))
                except Exception:
                    self.observation_errors += 1
                return value
            return call
        return decorate

    def recovering(self, original):
        @functools.wraps(original)
        def call(*args, **kwargs):
            self.recovery += 1
            self.emit('recovery_begin', cpu=cpu())
            try:
                result = original(*args, **kwargs)
            except BaseException as error:
                frames = []
                tb = error.__traceback__
                while tb:
                    if tb.tb_frame.f_code is original.__code__:
                        frames.append(dict(line=tb.tb_lineno, direct_locals=self.locals(tb.tb_frame)))
                    tb = tb.tb_next
                code = getattr(error, 'code', None)
                self.emit('recovery_error', error_type=type(error).__name__,
                          code=code if isinstance(code,str) and re.fullmatch('[a-z_]{1,80}',code) else None,
                          frames=frames, cpu=cpu())
                raise
            self.emit('recovery_end', code=result.get('code'), cpu=cpu())
            return result
        return call

    def __enter__(self):
        try:
            monitoring = sys.monitoring
            self.tool = next(i for i in (3,4) if monitoring.get_tool(i) is None)
            monitoring.use_tool_id(self.tool, 'tdb-readiness-observer')
            monitoring.register_callback(self.tool, monitoring.events.PY_RETURN, self.returned)
            def add(code):
                self.codes.append(code)
                monitoring.set_local_events(self.tool, code, monitoring.events.PY_RETURN)
                for child in code.co_consts:
                    if isinstance(child, CodeType) and child.co_name == 'read':
                        add(child)
            for method in (self.module.Controller._recover_locked, self.module.Controller._ready_process,
                           self.module._process_identity):
                add(method.__code__)
            self.emit('local_monitor_enabled')
        except Exception:
            self.emit('local_monitor_unavailable')
            self.disable()
        self.patch(self.module.Runtime, 'service', self.observed('systemd', lambda args, kwargs: {}))
        self.patch(self.module.Gate, 'request', self.observed('socket', lambda args, kwargs: {
            'action': next((a for a in ('status','release','prepare','commit') if a ==
                           (args[1] if len(args)>1 else kwargs.get('action'))), 'other')}))
        self.patch(self.module.Controller, '_recover_locked', self.recovering)
        return self

    def disable(self):
        if self.tool is not None:
            for code in self.codes:
                sys.monitoring.set_local_events(self.tool, code, 0)
            sys.monitoring.register_callback(self.tool, sys.monitoring.events.PY_RETURN, None)
            sys.monitoring.free_tool_id(self.tool)
            self.tool = None

    def __exit__(self, *_):
        for cls, name, original in reversed(self.patches):
            setattr(cls, name, original)
        self.disable()
        try:
            self.write(dict(format='tdb-readiness-trace-v1', records=self.records,
                            outcomes=self.outcomes, dropped=self.dropped, observation_errors=self.observation_errors))
        except Exception:
            print('readiness trace could not be saved', file=sys.stderr)
