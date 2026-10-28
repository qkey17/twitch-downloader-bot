"""Synthetic bot/data/CLI operations. No network clients, production data or secrets."""
import asyncio
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import select
import sqlite3
import sys
import time
from types import SimpleNamespace
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard

ROOT = guard.ROOT
CODE = ROOT / 'code'
CONFIG = ROOT / 'admin/daily.json'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def current():
    guard.no_links(ROOT / 'current.json')
    return guard.owned(Path(json.loads((ROOT / 'current.json').read_text())['case']))


def control():
    return json.loads(guard.owned(current() / 'control.json').read_text())


def update(**values):
    path = current() / 'control.json'
    value = control() | values
    temporary = path.with_name('control.new')
    with temporary.open('x') as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def event(name, **values):
    path = guard.owned(current() / 'events.jsonl')
    entry = json.dumps(dict(event=name, mono=time.monotonic(), pid=os.getpid(), **values)) + '\n'
    guard.require(len(entry) < 2048, 'oversize test event')
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, entry.encode())
    finally:
        os.close(fd)


def daily():
    return load(ROOT / 'admin/daily_backup.py', 'harness_daily')


def runtime():
    module = daily()
    config = module.Config(json.loads(CONFIG.read_text()))
    return module, config, module.Runtime(config, CONFIG)


def seal(path):
    path = guard.owned(path)
    files = {}
    for name in ('vod_history.db', 'result.html'):
        files[name] = hashlib.sha256((path / name).read_bytes()).hexdigest()
    manifest = json.dumps(dict(format='synthetic-harness-only', files=files), sort_keys=True).encode()
    (path / 'manifest.json').write_bytes(manifest)
    (path / 'COMPLETE').write_text(hashlib.sha256(manifest).hexdigest() + '\n')


def verify(path):
    path = guard.owned(path)
    manifest = (path / 'manifest.json').read_bytes()
    guard.require((path / 'COMPLETE').read_text().strip() == hashlib.sha256(manifest).hexdigest(),
                  'synthetic manifest mismatch')
    record = json.loads(manifest)
    guard.require(set(record['files']) == {'vod_history.db', 'result.html'}, 'bad synthetic files')
    for name, digest in record['files'].items():
        guard.no_links(path / name)
        guard.require(hashlib.sha256((path / name).read_bytes()).hexdigest() == digest, 'synthetic hash mismatch')
    with closing(sqlite3.connect(f'file:{path / "vod_history.db"}?mode=ro', uri=True)) as connection:
        guard.require(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'synthetic SQLite corrupt')


def barrier(phase):
    event(phase + '_begin')
    if control().get('block') == phase:
        # Always bounded even after a lost driver. SIGKILL tests kill this whole cgroup.
        deadline = time.monotonic() + (60 if control().get('powercut') else 25)
        while control().get('block') == phase and time.monotonic() < deadline:
            time.sleep(.1)
        guard.require(control().get('block') != phase, 'synthetic barrier expired')
    guard.require(control().get('fail') != phase, f'injected {phase} failure')


def tool(name, argv):
    guard.check()
    module, config, rt = runtime()
    def option(key):
        return Path(argv[argv.index(key) + 1])
    if name == 'backup_restore.py' and argv[0] == 'backup':
        state = rt.service()
        guard.require(module._stopped(state), 'snapshot before real service stopped')
        barrier('snapshot')
        destination = guard.owned(option('--destination'))
        guard.require(destination.parent == config.backup_dir, 'unexpected snapshot destination')
        destination.mkdir(mode=0o700)
        with closing(sqlite3.connect(f'file:{config.database}?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(destination / 'vod_history.db')) as target:
                source.backup(target)
        (destination / 'result.html').write_bytes((config.html_roots[0] / 'result.html').read_bytes())
        seal(destination)
        if control().get('fail_next_start'):
            update(start_fail=True)
        if control().get('next_ready_delay'):
            update(ready_delay=control()['next_ready_delay'])
        event('snapshot_done', state=state['ActiveState'], job=state['Job'])
        result = dict(status='ok')
    elif name == 'backup_restore.py' and argv[0] == 'verify':
        barrier('verify')
        verify(option('--backup'))
        event('verify_done')
        result = dict(status='ok')
    elif name == 'offsite_backup.py':
        # A local receipt, NEVER an archive/encryption/network/cloud operation.
        verify(option('--backup'))
        state = rt.service()
        with rt.gate(int(state['MainPID'])) as gate:
            reply = gate.status()
        guard.require(reply['running'] and reply['status'] == 'running' and reply['token'] is None
                      and not reply['committed'] and module._process_identity(state) is not None,
                      'offsite before application readiness')
        guard.require(module.Controller(config, rt)._owner() is None, 'offsite before recovery finished')
        barrier('offsite')
        result = dict(status='ok', cloud_verified=True, id=uuid.uuid4().hex, synthetic_only=True)
        event('offsite_done', main_pid=state['MainPID'], invocation=state['InvocationID'],
              restarts=state['NRestarts'])
    else:
        raise guard.Refused('unsupported synthetic tool operation')
    print(json.dumps(result))


async def bot():
    guard.check()
    event('bot_start', invocation=os.environ.get('INVOCATION_ID'))
    if control().get('start_fail'):
        event('bot_start_failed')
        return 23
    module = load(CODE / 'src/maintenance.py', 'harness_maintenance')
    app = SimpleNamespace(running=False, bot_data={})
    if control().get('busy'):
        app.bot_data['vod_global_operation'] = object()
    async def pause(_):
        event('prepare')
    async def resume(_):
        event('released')
    server = module.LocalServer(app, Path('/run/tdb-harness/control.sock'), pause, resume)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    loop.add_signal_handler(signal.SIGINT, stopped.set)
    delay = min(12, max(0, control().get('ready_delay', 0)))
    try:
        await asyncio.wait_for(stopped.wait(), delay)
    except TimeoutError:
        pass
    if not stopped.is_set():
        app.running = True
        await server.start()
        event('bot_ready')
        await stopped.wait()
    # The real gate stays closed for the entire synthetic graceful stop.
    server.coordinator.begin_shutdown()
    app.running = False
    event('bot_stopping')
    await asyncio.sleep(min(5, max(0, control().get('stop_delay', 0))))
    await asyncio.wait_for(server.close(), 5)
    event('bot_stopped')
    return 0


def controller(action):
    guard.check()
    module = daily()
    # Case 11 ONLY: inject failed state *reads*, keep every other manager interaction real.
    # In particular ensure_supervised, jobs, sudo start/stop and socket readiness are untouched.
    original = module.Runtime.service
    count = 0
    def service(self):
        nonlocal count
        mode = control().get('read_fault', 0)
        if mode == 'always' or count < mode:
            count += 1
            event('state_read_fault')
            raise OSError('injected temporary state read failure')
        return original(self)
    if control().get('read_fault'):
        module.Runtime.service = service
    module.main(['--config', str(CONFIG), action])


def controlled_lock(lock, path, emit, input_fd=0, watchdog=150):
    """Driver-owned lifetime; EOF/watchdog are failures, never successful release."""
    reason = 'error'
    metadata = {}
    try:
        with lock(path) as fd:
            info = os.fstat(fd)
            metadata = dict(st_dev=info.st_dev, st_ino=info.st_ino, holder_pid=os.getpid())
            deadline = time.monotonic() + watchdog
            emit('lock_held', **metadata)
            ready, _, _ = select.select([input_fd], [], [], watchdog)
            if not ready or time.monotonic() >= deadline:
                reason = 'watchdog'
                emit('lock_watchdog', **metadata)
                raise guard.Refused('deployment holder watchdog expired')
            if os.read(input_fd, 8) != b'release\n':
                reason = 'driver_closed_or_invalid_command'
                raise guard.Refused('deployment holder lost driver or invalid release')
            reason = 'driver_release'
            emit('lock_releasing', **metadata)
    finally:
        emit('lock_released', reason=reason, **metadata)


def state_action(action, *args):
    guard.check()
    module, config, rt = runtime()
    store = module.Store(config.state_dir)
    if action == 'seed-owner':
        data = rt.service()
        store.write('owner', dict(token=uuid.uuid4().hex, boot_id=rt.boot_id(),
                    invocation=data.get('InvocationID') or uuid.uuid4().hex,
                    pid=max(1, int(data.get('MainPID', '0'))), attempts=int(args[0]),
                    snapshot=str(config.backup_dir / 'synthetic-unfinished'), binding=config.binding))
    elif action == 'record':
        print(json.dumps(store.read(args[0])))
    elif action == 'hold':
        store.write('hold', args[0] == 'true')
    elif action == 'lock':
        path = config.deploy_lock if args[0] == 'deploy' else config.state_dir / 'offsite.lock'
        controlled_lock(module.file_lock, path, event)
    elif action == 'ready':
        print(json.dumps(module.Controller(config, rt)._ready()))
    else:
        raise guard.Refused('unknown synthetic state action')


def main():
    guard.check()
    action, *args = sys.argv[1:]
    if action == 'bot':
        sys.exit(asyncio.run(bot()))
    elif action == 'controller':
        controller(*args)
    else:
        state_action(action, *args)


if __name__ == '__main__':
    try:
        main()
    except guard.Refused as error:
        sys.exit(f'REFUSED: {error}')
