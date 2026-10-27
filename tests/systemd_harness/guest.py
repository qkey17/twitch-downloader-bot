"""Guest-only provisioning/cleanup. Every entry point checks VM isolation first."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

if sys.platform == 'linux':
    import pwd

# Explicit harness directory also works with the controller's -I -S interpreters.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard

ROOT = guard.ROOT
CODE = ROOT / 'code'
ADMIN = ROOT / 'admin'
CONFIG = ADMIN / 'daily.json'
USER = 'tdbh'
BASE = '0a9a453aa975615df1c0c0b6ca835f277d7dfe1c'
SOURCE = ('tools/daily_backup.py', 'tools/backup_restore.py', 'tools/offsite_backup.py',
          'src/maintenance.py')
HARNESS = ('guard.py', 'guest.py', 'synthetic.py', 'scenarios.py')


def command(args, *, timeout=15, check=True):
    return subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=timeout,
                          check=check, env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', HOME='/root'))


def systemctl(*args, check=True):
    guard.check()
    # Only this guest's manager; never accept an alternate --host/--machine/bus.
    return command(['/usr/bin/systemctl', *args], check=check)


def show(unit):
    guard.require(unit in guard.UNITS, 'unknown test unit')
    raw = systemctl('show', unit, '--no-pager').stdout
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    raise TimeoutError('bounded harness wait expired')


def private_write(path, value, *, user=True):
    path = guard.owned(path)
    content = value if isinstance(value, bytes) else json.dumps(value).encode()
    temporary = path.with_name(path.name + '.new')
    with temporary.open('xb') as output:
        os.chmod(temporary, 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    if user:
        account = pwd.getpwnam(USER)
        os.chown(temporary, account.pw_uid, account.pw_gid)
    os.replace(temporary, path)


def user_command(*args, check=True):
    guard.check()
    return command(['/usr/sbin/runuser', '-u', USER, '--', '/usr/bin/python3', '-I', '-S', '-B',
                    CODE / 'synthetic.py', *args], timeout=30, check=check)


def unit_texts():
    """Test units only; production templates are neither installed nor changed."""
    common = f'''User={USER}
Group={USER}
WorkingDirectory={ROOT}
Environment=HOME=/home/{USER}
Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin
UMask=0077
KillMode=control-group
Nice=10
CPUQuota=50%
MemoryHigh=96M
MemoryMax=160M
TasksMax=32
StandardOutput=journal
StandardError=journal
'''
    recover = f'/usr/bin/python3 -I -S -B {ADMIN}/daily_backup.py --config {CONFIG} recover'
    return {
        'tdb.service': f'''[Unit]
Description=ISOLATED HARNESS synthetic TDB
StartLimitIntervalSec=0
[Service]
{common}Type=simple
ExecStart=/usr/bin/python3 -I -S -B {CODE}/synthetic.py bot
Restart=on-failure
RestartSec=1s
TimeoutStopSec=infinity
KillSignal=SIGTERM
SendSIGKILL=no
RuntimeDirectory=tdb-harness
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=restart
[Install]
WantedBy=multi-user.target
''',
        'tdb-daily-backup.service': f'''[Unit]
Description=ISOLATED HARNESS real daily controller
OnFailure=tdb-backup-recover.service
After=tdb-backup-recover.service
[Service]
{common}Type=oneshot
ExecStart=/usr/bin/python3 -I -S -B {CODE}/synthetic.py controller run
ExecStopPost={recover}
Restart=no
TimeoutStartSec=120s
TimeoutStopSec=90s
SendSIGKILL=yes
''',
        'tdb-backup-recover.service': f'''[Unit]
Description=ISOLATED HARNESS real boot recovery
After=local-fs.target
StartLimitIntervalSec=0
[Service]
{common}Type=oneshot
ExecStart={recover}
Restart=no
TimeoutStartSec=90s
TimeoutStopSec=10s
[Install]
WantedBy=multi-user.target
''',
        'tdb-daily-backup.timer': '''[Unit]
Description=ISOLATED HARNESS persistent catch-up probe
[Timer]
OnCalendar=*-*-* *:*:00
Persistent=true
AccuracySec=1s
RandomizedDelaySec=0
Unit=tdb-daily-backup.service
[Install]
WantedBy=timers.target
''',
    }


def provision():
    data = guard.check(provision=True)
    guard.require(not ROOT.exists(), 'harness already exists; use a new disposable VM')
    guard.require(command(['/usr/bin/systemctl', '--version']).stdout.startswith('systemd 255'),
                  'requires systemd 255')
    # Refuse unit/user collisions BEFORE writing anything, including aliases in /usr/lib.
    for unit in guard.UNITS:
        raw = command(['/usr/bin/systemctl', 'show', unit, '--property=LoadState', '--value'], check=False)
        guard.require(raw.stdout.strip() == 'not-found', 'existing TDB unit found')
        for base in ('/etc/systemd/system', '/run/systemd/system'):
            path = Path(base) / unit
            guard.require(not path.exists() and not path.is_symlink(), 'existing unit path found')
    try:
        pwd.getpwnam(USER)
    except KeyError:
        pass
    else:
        raise guard.Refused('test user already exists')
    sudoers = Path('/etc/sudoers.d/tdb-harness')
    guard.require(not sudoers.exists() and not sudoers.is_symlink(), 'sudoers collision')
    payload = Path(__file__).resolve().parent
    manifest = json.loads((payload / 'bundle.json').read_text())
    expected = {*SOURCE, *HARNESS}
    guard.require(manifest['base'] == BASE and set(manifest['sha256']) == expected, 'wrong bundle')
    for name in expected:
        path = payload / name
        guard.no_links(path)
        guard.require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()
                      == manifest['sha256'][name], 'bundle checksum mismatch')
    ROOT.mkdir(mode=0o755)
    command(['/usr/sbin/useradd', '--create-home', '--user-group', '--shell', '/usr/sbin/nologin', USER])
    account = pwd.getpwnam(USER)
    os.chmod(f'/home/{USER}', 0o700)
    for directory in (CODE, ADMIN, ROOT / 'cases'):
        directory.mkdir(mode=0o755 if directory == CODE else 0o700)
        if directory != CODE:
            os.chown(directory, account.pw_uid, account.pw_gid)
    for name in expected:
        target = CODE / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write((payload / name).read_bytes())
        os.chmod(target, 0o644)
    shutil.copyfile(CODE / 'tools/daily_backup.py', ADMIN / 'daily_backup.py')
    for name in ('backup_restore.py', 'offsite_backup.py'):
        # Real helper functions remain importable; ONLY CLI actions are synthetic.
        text = f'''import sys
sys.path.insert(0, {str(CODE)!r})
import synthetic
if __name__ == '__main__':
    synthetic.tool({name!r}, sys.argv[1:])
else:
    _real = synthetic.load({str(CODE / 'tools' / name)!r}, 'real_{name[:-3]}')
    globals().update({{k: v for k, v in vars(_real).items() if not k.startswith('__')}})
'''
        (ADMIN / name).write_text(text)
    for path in ADMIN.iterdir():
        os.chmod(path, 0o600)
        os.chown(path, account.pw_uid, account.pw_gid)
    installed = {}
    for name, content in unit_texts().items():
        path = Path('/etc/systemd/system') / name
        with path.open('x') as output:
            output.write(content)
        os.chmod(path, 0o644)
        installed[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    with sudoers.open('x') as output:
        output.write(f'{USER} ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block start tdb.service, '
                     '/usr/bin/systemctl --no-block stop tdb.service\n')
    os.chmod(sudoers, 0o440)
    command(['/usr/sbin/visudo', '-cf', sudoers])
    installed[str(sudoers)] = hashlib.sha256(sudoers.read_bytes()).hexdigest()
    # Root-owned, public identity only; no credentials or host environment.
    guard.ATTEST.write_text(json.dumps(dict(format='tdb-harness-v1', machine=data['machine'],
                                          base=BASE, installed=installed)))
    os.chmod(guard.ATTEST, 0o644)
    systemctl('daemon-reload')
    systemctl('enable', 'tdb-backup-recover.service')
    print('PROVISIONED: test units disabled except boot recovery; now run scenarios.py run')


def cleanup():
    guard.check()
    guard.require(os.geteuid() == 0, 'cleanup requires guest root')
    record = json.loads(guard.ATTEST.read_text())
    for name, checksum in record['installed'].items():
        guard.no_links(Path(name))
        guard.require(hashlib.sha256(Path(name).read_bytes()).hexdigest() == checksum,
                      'installed harness resource changed; cleanup refused')
    # No directory deletion. Keep test evidence for inspection. Stop only owned units.
    systemctl('disable', 'tdb-daily-backup.timer', 'tdb-backup-recover.service', 'tdb.service')
    systemctl('stop', '--no-block', 'tdb-daily-backup.timer', 'tdb-daily-backup.service')
    wait(lambda: show('tdb-daily-backup.service')['ActiveState'] in ('inactive', 'failed'), 85)
    systemctl('stop', '--no-block', 'tdb-backup-recover.service')
    wait(lambda: show('tdb-backup-recover.service')['ActiveState'] in ('inactive', 'failed'), 85)
    systemctl('stop', '--no-block', 'tdb.service')
    wait(lambda: show('tdb.service')['ActiveState'] in ('inactive', 'failed'), 12)
    for unit in guard.UNITS:
        state = show(unit)
        guard.require(state['ActiveState'] in ('inactive', 'failed') and state.get('Job', '0') in ('', '0')
                      and state.get('MainPID', '0') == '0' and state.get('ControlPID', '0') == '0',
                      f'cleanup incomplete: {unit}')
        group = state.get('ControlGroup', '')
        if group:
            path = Path('/sys/fs/cgroup') / group.lstrip('/')
            guard.require(path.is_relative_to('/sys/fs/cgroup/system.slice'), 'unexpected cgroup')
            if path.exists():
                guard.require(all(not p.read_text().strip() for p in path.rglob('cgroup.procs')),
                              'remaining processes in harness cgroup')
    print('CLEANUP_OK: no active test units/jobs; evidence retained, no directories deleted')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('provision', 'cleanup', 'check'))
    args = parser.parse_args()
    try:
        guard.check(provision=args.action == 'provision')
        guard.require(os.geteuid() == 0, 'guest administration requires root')
        if args.action == 'check':
            print('ISOLATED_GUEST_OK')
        else:
            globals()[args.action]()
    except (guard.Refused, OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f'REFUSED/FAILED: {error}')
