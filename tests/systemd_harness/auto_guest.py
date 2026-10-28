"""Root driver in the disposable offline guest. No login or host commands."""
import base64
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard
import guest
import scenarios
import update
import wire
import release

ROOT = guard.ROOT
PORT = Path('/dev/virtio-ports/org.tdb.harness')


class Channel:
    def __init__(self, config):
        info = PORT.stat()
        guard.require(stat.S_ISCHR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                      'unsafe virtio control port')
        self.fd = os.open(PORT, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        self.config = config
        self.sent = set()

    def close(self):
        os.close(self.fd)

    def send(self, kind, **fields):
        content = wire.encode(dict(run_id=self.config['run_id'], type=kind, **fields))
        deadline = time.monotonic()+60
        while content:
            if time.monotonic() >= deadline:
                raise TimeoutError('host control channel not draining')
            try:
                count = os.write(self.fd, content)
                content = content[count:]
            except BlockingIOError:
                select.select([], [self.fd], [], .1)

    def handshake(self, phase, version):
        self.send('hello', phase=phase, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  base=version['base'], harness=version['harness'], python=sys.version.split()[0],
                  systemd=guest.command(['/usr/bin/systemctl', '--version']).stdout.splitlines()[0])
        buffer, deadline = bytearray(), time.monotonic()+120
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], .5)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            buffer.extend(chunk)
            if len(buffer) > wire.MAX_LINE:
                raise ValueError('invalid host handshake')
            if buffer.endswith(b'\n'):
                reply = wire.decode(buffer)
                guard.require(reply == dict(type='host_ready', run_id=self.config['run_id']), 'wrong host session')
                return
        raise TimeoutError('host handshake timeout')

    def file(self, name, content):
        wire.valid_name(name)
        if name in self.sent:
            return  # Keep the first immutable evidence; failure is a separate event.
        if len(content) > wire.MAX_FILE:
            raise ValueError('guest artifact exceeds limit')
        self.send('file_begin', name=name, size=len(content), sha256=hashlib.sha256(content).hexdigest())
        for index in range(0, len(content), wire.CHUNK):
            self.send('file_chunk', data=base64.b64encode(content[index:index+wire.CHUNK]).decode('ascii'))
        self.send('file_end')
        self.sent.add(name)


def install(config):
    """Called once by a checksummed NoCloud bootstrap, inside a fresh VM."""
    guard.check(provision=True)
    os.umask(0o022)  # Existing provisioning expects root-owned 0755 code directories.
    # Reproduce the reported cold/disabled installation, then exercise the REAL
    # updater on systemd 255 before running the new controller scenarios.
    payload = Path(__file__).resolve().parent
    content = (payload / 'legacy.json').read_bytes()
    guard.require(hashlib.sha256(content).hexdigest() == config['legacy_sha256'], 'legacy transfer checksum mismatch')
    legacy = json.loads(content)
    guard.require(set(legacy) == set(release.OLD_HASHES), 'unknown legacy fixture files')
    directory = Path('/run/tdb-legacy')
    directory.mkdir(mode=0o700)
    hashes = {}
    for name, encoded in legacy.items():
        data = base64.b64decode(encoded, validate=True)
        sha = hashlib.sha256(data).hexdigest()
        guard.require(sha in release.OLD_HASHES[name], 'modified legacy source')
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        hashes[name] = sha
    (directory / 'bundle.json').write_text(json.dumps(dict(base=release.OLD_BASE, sha256=hashes)))
    for action in ('provision', 'cleanup'):
        subprocess.run(['/usr/bin/python3', '-I', '-S', '-B', str(directory / 'guest.py'), action],
                       check=True, timeout=180)
    with update.lock():
        update.apply(payload)
    guest.private_write(ROOT / 'updater-preflight.json', dict(status='PASS',
        states={unit: update.service_state(unit) for unit in guard.UNITS}), user=False)
    guest.private_write(ROOT / 'auto-config.json', config, user=False)
    guest.private_write(ROOT / 'auto-state.json', dict(stage='suite'), user=False)
    journal_dir = Path('/etc/systemd/journald.conf.d')
    journal_dir.mkdir(exist_ok=True)
    with (journal_dir / 'tdb-harness.conf').open('x') as output:
        output.write('[Journal]\nStorage=persistent\nSystemMaxUse=32M\nRuntimeMaxUse=8M\n')
    guest.command(['/usr/bin/systemd-tmpfiles', '--create', '--prefix=/var/log/journal'])
    guest.systemctl('restart', 'systemd-journald.service')
    unit = Path('/etc/systemd/system/tdb-harness-auto.service')
    with unit.open('x') as output:
        output.write('''[Unit]
Description=Disposable TDB integration driver
After=local-fs.target tdb-backup-recover.service tdb.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -S -B /var/lib/tdb-harness/code/auto_guest.py
TimeoutStartSec=35min
TimeoutStopSec=30s
KillMode=control-group
MemoryMax=256M
TasksMax=64
Restart=no
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
''')
    guest.systemctl('daemon-reload')
    guest.systemctl('enable', 'tdb-harness-auto.service')
    guest.systemctl('start', '--no-block', 'tdb-harness-auto.service')


def artifacts(channel, phase):
    guard.check()
    files = [(p, f'{phase}/reports/{p.name}') for p in ROOT.glob('report-*.json')]
    for name in ('version.json', 'auto-state.json', 'powercut.json', 'reboot-result.json', 'updater-preflight.json'):
        path = ROOT / name
        if path.exists():
            files.append((path, f'{phase}/metadata/{name}'))
    for case in (ROOT / 'cases').iterdir():
        if not case.is_dir() or len(case.name) != 32:
            continue
        for name in ('events.jsonl', 'control.json'):
            path = case / name
            if path.exists():
                files.append((path, f'{phase}/cases/{case.name}/{name}'))
        for name in ('owner', 'status', 'day', 'hold', 'candidate', 'pending', 'receipt'):
            path = case / 'state' / (name + '.json')
            if path.exists():
                files.append((path, f'{phase}/cases/{case.name}/{name}.json'))
    for path, name in files:
        channel.file(name, update.read_file(path)[0])
    for boot in ('0', '-1') if phase == 'resume' else ('0',):
        for unit in (*guard.UNITS, 'tdb-harness-auto.service'):
            result = guest.command(['/usr/bin/journalctl', '-u', unit, '-b', boot, '--no-pager',
                                    '-n', '2000', '--output=short-monotonic'], check=False)
            # Synthetic units only. Never export environment, general host logs,
            # private config, SSH keys, SQLite contents or arbitrary filesystem paths.
            channel.file(f'{phase}/journals/{unit}-boot{boot}.log', result.stdout.encode()[-wire.MAX_FILE:])


def main():
    guard.check()
    guard.require(os.geteuid() == 0, 'automation requires guest root')
    config = json.loads((ROOT / 'auto-config.json').read_text())
    state = json.loads((ROOT / 'auto-state.json').read_text())
    phase = state['stage']
    version = update.verify()
    guard.require(version['base'] == config['base'] and version['harness'] == config['harness'], 'wrong installed version')
    channel = Channel(config)
    failed = True
    export_phase = 'resume' if phase == 'resume' else 'suite'
    try:
        channel.handshake(phase, version)
        with update.lock(shared=True):
            if phase == 'suite':
                report = scenarios.run(progress=lambda name, status: channel.send('case', name=name, status=status))
                artifacts(channel, 'suite')
                channel.send('suite_pass', report=report.name)
                export_phase = 'powercut'
                # Export bulk diagnostics before arming the short crash barrier.
                scenarios.stage_powercut()
                guest.private_write(ROOT / 'auto-state.json', dict(stage='resume'), user=False)
                os.sync()
                checkpoint = update.read_file(ROOT / 'powercut.json')[0]
                channel.file('powercut/metadata/powercut.json', checkpoint)
                channel.send('powercut_ready', boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
                # The HOST kills the whole QEMU process. Never substitute a
                # service restart, guest shutdown, or an in-process simulation.
                time.sleep(30)
                raise TimeoutError('host did not cut VM power')
            elif phase == 'resume':
                scenarios.resume()
                artifacts(channel, 'resume')
                channel.send('complete', success=True)
                failed = False
            else:
                raise ValueError('unexpected persisted stage; no automatic retries')
    except BaseException as error:
        try:
            guest.cleanup()
            artifacts(channel, export_phase)
        except BaseException as cleanup_error:
            channel.send('diagnostic', error=type(cleanup_error).__name__)
        channel.send('complete', success=False, error=type(error).__name__, detail=str(error)[:1500])
    finally:
        channel.close()
    # Normal final poweroff is separate from scenario 14's forced host kill.
    guest.systemctl('disable', 'tdb-harness-auto.service')
    guest.systemctl('poweroff', '--no-block')
    return 1 if failed else 0


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('driver interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
