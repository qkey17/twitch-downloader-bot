"""Short, nonblocking admission using the existing daily lease -> deploy lock order.

Never create administrative files. No lease spans network/worker awaits.
"""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import shutil
import stat
from .file_lock import try_lock_file


@contextmanager
def start_guard(*, check_resources=True):
    from . import config
    with ExitStack() as stack:
        try:
            # The installed helper supports only this documented daily binding.
            state = Path.home() / 'tdb-daily-state'
            installed = Path.home() / 'tdb-admin/daily-backup.json'
            if os.environ.get('TCD_MAINTENANCE_SOCKET') and (
                    state.exists() or state.is_symlink() or installed.exists() or installed.is_symlink()):
                if os.name != 'posix':
                    raise ValueError('unsupported_administration_platform')
                info = state.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('unsafe_state_directory')
                for p in (state/'offsite.lock', Path.home()/'.local/state/tdb-deploy/deploy.lock'):
                    info = p.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.getuid() or info.st_mode & 0o077):
                        raise ValueError('unsafe_admission_lock')
                    fd = os.open(p, os.O_RDWR | os.O_NOFOLLOW)
                    f = stack.enter_context(os.fdopen(fd, 'r+b'))
                    after = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino) or not try_lock_file(f):
                        raise ValueError('admission_lock_busy')
                for name in ('hold', 'owner', 'pending', 'candidate'):
                    p = state/(name+'.json')
                    if not p.exists() and not p.is_symlink():
                        if name == 'hold':
                            raise ValueError('missing_hold')
                        continue
                    info = p.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536):
                        raise ValueError('unsafe_admission_state')
                    with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW), 'rb') as f:
                        after = os.fstat(f.fileno())
                        if (info.st_dev,info.st_ino) != (after.st_dev,after.st_ino):
                            raise ValueError('state_replaced')
                        raw = f.read(65537)
                    def unique(pairs):
                        data = {}
                        for k,v in pairs:
                            if k in data:
                                raise ValueError('duplicate_state_key')
                            data[k] = v
                        return data
                    if len(raw)>65536:
                        raise ValueError('state_limit')
                    data = json.loads(raw,object_pairs_hook=unique)
                    if set(data) != {'format','value'} or data['format'] != 'tdb-daily-state-v1':
                        raise ValueError('invalid_admission_state')
                    if name == 'hold' and data['value'] is not False or name != 'hold' and data['value'] is not None:
                        raise ValueError('admission_closed')
            for value in ((config.SPOOL_DIR, config.OUT_DIR, config.PUBLIC_HTML_DIR) if check_resources else ()):
                if not value:
                    continue
                path = Path(value)
                while not path.exists():
                    path = path.parent
                if shutil.disk_usage(path).free < config.HTML_CLEANUP_LOW_BYTES:
                    raise ValueError('disk_reserve')
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            yield False
        else:
            yield True
