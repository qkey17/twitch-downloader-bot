"""Offline bundle + fixed, unprivileged QEMU launcher. Never calls systemctl."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

BASE = '0a9a453aa975615df1c0c0b6ca835f277d7dfe1c'
SOURCE = ('tools/daily_backup.py', 'tools/backup_restore.py', 'tools/offsite_backup.py',
          'src/maintenance.py')
HARNESS = ('guard.py', 'guest.py', 'synthetic.py', 'scenarios.py')


def check_host():
    """Avoid even VM resource consumption on the small production host."""
    for name in ('/home/ubuntu/tdb-admin', '/home/ubuntu/twitch-downloader-bot', '/var/www/tdb'):
        if Path(name).exists():
            raise ValueError('production paths present: use a separate test workstation')
    if sys.platform == 'linux':
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        if int(memory['MemAvailable'].split()[0]) < 2 * 1024**2:
            raise ValueError('test workstation needs at least 2 GiB available RAM')


def safe_path(path):
    path = Path(path).absolute()
    if any(c in str(path) for c in ',\n\r') or path.is_symlink():
        raise ValueError('QEMU paths must not contain commas/newlines or be symlinks')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('symlink parent refused')
    return path


def bundle(destination):
    """Explicit allowlist; no .git, .env, credentials, working data or dependencies."""
    repo = Path(__file__).resolve().parents[2]
    destination = safe_path(destination)
    destination.mkdir(mode=0o700)  # Exclusive, never overwrite an existing bundle.
    hashes = {}
    for name in SOURCE:
        # Export the audited commit, not accidentally changed production sources.
        content = subprocess.run(['git', 'show', f'{BASE}:{name}'], cwd=repo,
                                 check=True, capture_output=True).stdout
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    for name in HARNESS:
        content = Path(__file__).with_name(name).read_bytes()
        (destination / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (destination / 'bundle.json').write_text(json.dumps(dict(base=BASE, sha256=hashes), indent=2))


def qemu_argv(directory, iso=None, gui=False, install=False):
    directory = safe_path(directory)
    record = json.loads((directory / 'vm.json').read_text())
    identity = str(uuid.UUID(record['uuid']))
    disk = safe_path(directory / 'guest.qcow2')
    if not disk.is_file():
        raise ValueError('missing harness disk')
    args = ['qemu-system-x86_64', '-nodefaults', '-machine', 'q35', '-accel', 'tcg',
            '-m', '954', '-smp', '1', '-serial', 'mon:stdio',
            '-nic', 'none', '-no-reboot', '-smbios',
            f'type=1,manufacturer=TDB-Harness,product=TDB-Isolated-VM,uuid={identity}',
            '-drive', f'file={disk},format=qcow2,if=virtio']
    if not gui:
        args += ['-display', 'none']
    else:
        args += ['-device', 'VGA']  # -nodefaults also removes the default video device.
    if iso:
        iso = safe_path(iso)
        if not iso.is_file():
            raise ValueError('missing read-only ISO')
        args += ['-drive', f'file={iso},format=raw,media=cdrom,readonly=on']
    if install:
        if not iso:
            raise ValueError('installer requires a read-only Ubuntu ISO')
        args += ['-boot', 'once=d']
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('bundle', 'init', 'run', 'plan'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--iso', type=Path)
    parser.add_argument('--gui', action='store_true', help='local installer display; no remote display server')
    parser.add_argument('--install', action='store_true', help='boot the Ubuntu installer CD once')
    args = parser.parse_args()
    if args.action in ('init', 'run'):
        check_host()
    if args.action == 'bundle':
        bundle(args.directory)
    elif args.action == 'init':
        directory = safe_path(args.directory)
        directory.mkdir(mode=0o700)  # Must be a NEW directory on a test workstation.
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(directory / 'guest.qcow2'), '32G'],
                       check=True, timeout=30)
        (directory / 'vm.json').write_text(json.dumps(dict(uuid=str(uuid.uuid4()))))
    else:
        command = qemu_argv(args.directory, args.iso, args.gui, args.install)
        if args.action == 'plan':
            print(json.dumps(command))
        else:
            # Foreground serial console. No NIC, host filesystem, bus or device passthrough.
            subprocess.run(command, check=True)


if __name__ == '__main__':
    main()
