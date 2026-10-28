"""Offline bundle + fixed, unprivileged QEMU launcher. Never calls systemctl."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release
import iso

BASE, SOURCE, HARNESS = release.BASE, release.SOURCE, release.HARNESS


def git_bytes(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True).stdout


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
    if destination.exists():
        raise FileExistsError('bundle destination already exists')
    commit = git_bytes(repo, 'rev-parse', 'HEAD').decode().strip()
    if len(commit) != 40 or git_bytes(repo, 'diff', 'HEAD', '--', 'tests/systemd_harness').strip():
        raise ValueError('commit the reviewed harness before building a bundle')
    destination.mkdir(mode=0o700)  # Exclusive, never overwrite an existing bundle.
    hashes = {}
    for name in SOURCE:
        # Export the audited commit, not accidentally changed production sources.
        content = git_bytes(repo, 'show', f'{BASE}:{name}')
        if hashlib.sha256(content).hexdigest() != release.SOURCE_HASHES[name]:
            raise ValueError('source is not the reviewed commit')
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    for name in HARNESS:
        content = git_bytes(repo, 'show', f'{commit}:tests/systemd_harness/{name}')
        (destination / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (destination / 'bundle.json').write_text(json.dumps(dict(base=BASE, harness=commit,
        old_base=release.OLD_BASE, old_harness=release.OLD_HARNESS, sha256=hashes), indent=2))


def prepare_update(repo):
    check_host()
    name = 'tdb-update-75d1654-' + uuid.uuid4().hex[:12]
    directory = repo.parent / name
    bundle(directory)
    manifest = json.loads((directory / 'bundle.json').read_text())
    destination = directory.with_suffix('.iso')
    iso.create(directory, destination, [*manifest['sha256'], 'bundle.json'])
    with destination.open('rb') as source:
        checksum = hashlib.file_digest(source, 'sha256').hexdigest()
    receipt = dict(iso=str(destination), sha256=checksum,
                   harness=manifest['harness'], base=BASE)
    work = repo / '_work'
    work.mkdir(mode=0o700, exist_ok=True)
    (work / 'update-iso.json').write_text(json.dumps(receipt))
    print('ISO_READY ' + str(destination))
    print('ISO_SHA256 ' + receipt['sha256'])
    print('SOURCE ' + BASE + ' HARNESS ' + manifest['harness'])
    print('After shutting down ONLY the guest, run: tests\\systemd_harness\\update.cmd run')


def run_update(repo):
    check_host()
    if not (repo.parent / 'vm' / 'vm.json').is_file():
        raise ValueError('existing vm/vm.json missing; never create a replacement VM')
    receipt = json.loads((repo / '_work/update-iso.json').read_text())
    if receipt['harness'] != git_bytes(repo, 'rev-parse', 'HEAD').decode().strip():
        raise ValueError('ISO was built by another harness commit; build again')
    path = safe_path(receipt['iso'])
    with path.open('rb') as source:
        checksum = hashlib.file_digest(source, 'sha256').hexdigest()
    if receipt['base'] != BASE or checksum != receipt['sha256']:
        raise ValueError('ISO changed; rebuild from reviewed commit')
    executable = shutil.which('qemu-system-x86_64')
    if not executable and sys.platform == 'win32':
        candidate = Path('C:/Program Files/qemu/qemu-system-x86_64.exe')
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise ValueError('QEMU executable not found; no installation attempted')
    command = qemu_argv(repo.parent / 'vm', path)
    command[0] = executable
    subprocess.run(command, check=True)


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
    parser.add_argument('action', choices=('bundle', 'init', 'run', 'plan', 'prepare-update', 'run-update'))
    parser.add_argument('directory', type=Path, nargs='?')
    parser.add_argument('--iso', type=Path)
    parser.add_argument('--gui', action='store_true', help='local installer display; no remote display server')
    parser.add_argument('--install', action='store_true', help='boot the Ubuntu installer CD once')
    args = parser.parse_args()
    if args.action in ('prepare-update', 'run-update'):
        repo = Path(__file__).resolve().parents[2]
        {'prepare-update': prepare_update, 'run-update': run_update}[args.action](repo)
        return
    if args.directory is None:
        parser.error('directory is required')
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
