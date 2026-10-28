"""Fail closed before any manager command. Only for the disposable QEMU guest."""
import json
import os
from pathlib import Path
import platform
import socket
import stat
import struct
import sys

ROOT = Path('/var/lib/tdb-harness')
ATTEST = ROOT / 'guest.json'
LABEL = 'TDB-HARNESS'
UNITS = ('tdb.service', 'tdb-daily-backup.service', 'tdb-backup-recover.service',
         'tdb-daily-backup.timer')
PRODUCTION = ('/home/ubuntu/tdb-admin', '/home/ubuntu/twitch-downloader-bot',
              '/home/ubuntu/tdb-backups', '/home/ubuntu/tdb-daily-state',
              '/home/ubuntu/tdb-offsite', '/var/www/tdb')


class Refused(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def no_links(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'unsafe path')
    for part in (*reversed(path.parents), path):
        require(not part.is_symlink(), 'symlink in harness path')


def evidence():
    """Read-only probes. No subprocess, manager operation or filesystem write."""
    require(sys.platform == 'linux', 'requires the isolated Linux VM')
    mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    root = next(m for m in mounts if m[4] == '/')
    device = Path('/dev/disk/by-label') / LABEL
    require(device.exists(), 'missing dedicated root filesystem label')
    dev = device.stat().st_rdev
    return dict(
        manufacturer=Path('/sys/class/dmi/id/sys_vendor').read_text().strip(),
        product=Path('/sys/class/dmi/id/product_name').read_text().strip(),
        machine=Path('/etc/machine-id').read_text().strip(),
        pid1=Path('/proc/1/comm').read_text().strip(),
        # Ubuntu restricts /proc/1/ns to root. The unprivileged fixture trusts the
        # root-owned machine attestation; the root driver always checks both peers.
        namespace=os.geteuid() != 0 or os.readlink('/proc/self/ns/pid') == os.readlink('/proc/1/ns/pid'),
        root_device=root[2] == f'{os.major(dev)}:{os.minor(dev)}',
        root_type=root[root.index('-') + 1],
        shared=any(m[m.index('-') + 1] in ('9p', 'virtiofs', 'nfs', 'nfs4', 'cifs', 'fuse.sshfs')
                   for m in mounts),
        container=Path('/.dockerenv').exists() or Path('/run/systemd/container').exists(),
        network=sorted(p.name for p in Path('/sys/class/net').iterdir()),
        ubuntu=Path('/etc/os-release').read_text(),
        python=tuple(sys.version_info[:2]),
        production=any(Path(p).exists() or Path(p).is_symlink() for p in PRODUCTION),
    )


def validate(data):
    """Pure predicate, separately tested on the host with synthetic evidence."""
    require(data['manufacturer'] == 'TDB-Harness' and data['product'] == 'TDB-Isolated-VM',
            'not the dedicated QEMU machine')
    require(data['pid1'] == 'systemd' and data['namespace'], 'not the guest system manager')
    require(data['root_device'] and data['root_type'] == 'ext4', 'wrong root filesystem')
    require(not data['shared'] and not data['container'], 'shared filesystem/container refused')
    require(data['network'] == ['lo'], 'guest must have no network device')
    require('ID=ubuntu\n' in data['ubuntu'] and 'VERSION_ID="24.04"' in data['ubuntu'],
            'requires Ubuntu 24.04')
    require(data['python'] == (3, 12), 'requires Python 3.12')
    require(not data['production'], 'production paths found')
    require(len(data['machine']) == 32 and all(c in '0123456789abcdef' for c in data['machine']),
            'invalid guest machine-id')


def upgrade_fence():
    pointer = ROOT / 'update-current.json'
    status = ROOT / 'update-status.json'
    if not status.exists() and not status.is_symlink():
        require(not pointer.exists() and not pointer.is_symlink(), 'upgrade status missing: run updater verify/recover')
        return
    # Only this small root-owned status is public. Unprivileged tdbh fixtures
    # must never need access to the private transaction/backups directory.
    no_links(status)
    info = status.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and info.st_nlink == 1 and info.st_size <= 4096, 'unsafe upgrade status')
    transaction = json.loads(status.read_text())
    txid = transaction['id']
    require(isinstance(txid, str) and len(txid) == 32 and all(c in '0123456789abcdef' for c in txid),
            'invalid upgrade ID')
    require(set(transaction) == {'id', 'state'} and transaction['state'] in ('committed', 'rolled_back'),
            'upgrade incomplete: run the saved updater recover command')


def check(*, provision=False, upgrading=False):
    data = evidence()
    validate(data)
    no_links(ROOT)
    if os.geteuid() == 0:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect('/run/systemd/private')
            pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            require(pid == 1 and uid == 0, 'systemd socket does not belong to guest PID 1')
    require(not provision or os.geteuid() == 0, 'provision requires guest root')
    if not provision:
        parent = ROOT.stat()
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and not parent.st_mode & 0o022,
                'unsafe harness installation directory')
        no_links(ATTEST)
        info = ATTEST.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                and info.st_nlink == 1 and info.st_size <= 16384, 'unsafe guest attestation')
        record = json.loads(ATTEST.read_text())
        require(record['machine'] == data['machine'] and record['format'] == 'tdb-harness-v1',
                'guest attestation mismatch')
        if not upgrading:
            upgrade_fence()
    return data


def owned(path):
    """Restrict test data writes/removals to this installation, without symlinks."""
    path = Path(path)
    no_links(path)
    require(path.is_relative_to(ROOT) and path != ROOT, 'outside harness root')
    return path


if __name__ == '__main__':
    try:
        check()
        print('ISOLATED_GUEST_OK')
    except (Refused, OSError, ValueError, KeyError) as error:
        sys.exit(f'REFUSED: {error}')
