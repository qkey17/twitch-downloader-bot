"""One offline, journaled upgrade of the disposable harness. No production actions."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard
import release

ROOT = guard.ROOT
LIMIT = 2 * 1024**2
STAGING = None


@contextmanager
def staging(directory):
    global STAGING
    previous, STAGING = STAGING, directory
    try:
        yield
    finally:
        STAGING = previous


def digest(data):
    return hashlib.sha256(data).hexdigest()


def attributes(info):
    return dict(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)


def stamp(info):
    # Reading may legitimately update atime on ext4.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_file(path):
    guard.no_links(path)
    before = path.stat()
    guard.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= LIMIT,
                  'unsafe/oversize update file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        guard.require(stamp(info) == stamp(before), 'update file changed before open')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            data = source.read(LIMIT + 1)
        guard.require(len(data) <= LIMIT and stamp(os.fstat(fd)) == stamp(info) and stamp(path.stat()) == stamp(info),
                      'update file changed during read')
    finally:
        os.close(fd)
    return data, attributes(info)


def sync_dir(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def set_attributes(fd, meta):
    os.fchown(fd, meta['uid'], meta['gid'])
    os.fchmod(fd, meta['mode'])


def atomic(path, data, meta):
    guard.no_links(path)
    temporary = (STAGING or path.parent) / ('.update-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            fd = None  # fdopen now owns it; close before the atomic rename.
            output.write(data)
            output.flush()
            set_attributes(output.fileno(), meta)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()  # Only this invocation's exclusive temporary file.


def write_json(path, value):
    atomic(path, json.dumps(value, sort_keys=True).encode(), dict(mode=0o600, uid=0, gid=0))


def publish_status(transaction):
    atomic(ROOT / 'update-status.json', json.dumps({k: transaction[k] for k in ('id', 'state')}).encode(),
           dict(mode=0o644, uid=0, gid=0))


def root_check():
    # All isolation checks remain mandatory; only the incomplete-update fence
    # is bypassed so the saved updater can repair that very transaction.
    guard.check(upgrading=True)
    guard.require(os.geteuid() == 0, 'upgrade requires guest root')


@contextmanager
def lock(shared=False):
    root_check()
    import fcntl
    path = ROOT / 'update.lock'
    guard.no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        guard.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == 0
                      and not info.st_mode & 0o077 and path.stat() == info, 'unsafe update lock')
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError:
            raise guard.Refused('another harness/update process owns the lock') from None
        yield
    finally:
        os.close(fd)


def controller_paths():
    return {'code/' + name: name for name in (*release.SOURCE, *release.HARNESS)} | {
        'admin/daily_backup.py': 'tools/daily_backup.py'}


def wrappers():
    return {f'admin/{name}': release.wrapper(name) for name in ('backup_restore.py', 'offsite_backup.py')}


def account():
    import pwd
    user = pwd.getpwnam('tdbh')
    return user.pw_uid, user.pw_gid


def expected_attributes(relative):
    uid, gid = account() if relative.startswith('admin/') else (0, 0)
    return dict(mode=0o600 if relative.startswith('admin/') else 0o644, uid=uid, gid=gid)


def check_directories():
    uid, gid = account()
    for name in ('code', 'code/tools', 'code/src', 'admin', 'updates'):
        path = ROOT / name
        guard.no_links(path)
        if name == 'updates' and not path.exists():
            continue
        info = path.stat()
        owner = (uid, gid) if name == 'admin' else (0, 0)
        mode = 0o700 if name in ('admin', 'updates') else 0o755
        guard.require(stat.S_ISDIR(info.st_mode) and attributes(info) == dict(mode=mode, uid=owner[0], gid=owner[1])
                      and info.st_dev == ROOT.stat().st_dev,
                      'unsafe installation directory: ' + name)


def service_state(unit):
    raw = subprocess.run(['/usr/bin/systemctl', 'show', unit, '--no-pager'], check=True,
                         capture_output=True, text=True, timeout=15,
                         env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', HOME='/root')).stdout
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def check_units():
    # Read real manager state through the existing guarded helper. Never start,
    # stop, reset, enable, mask or remove a unit during an update.
    for unit in guard.UNITS:
        state = service_state(unit)
        guard.require(state.get('LoadState') == 'loaded' and state.get('ActiveState') in ('inactive', 'failed')
                      and state.get('Job') in ('', '0') and state.get('MainPID', '0') == '0'
                      and state.get('ControlPID', '0') == '0', 'test unit/job is active or unknown')
        allowed = ('static', 'disabled') if unit == 'tdb-daily-backup.service' else ('disabled',)
        guard.require(state.get('UnitFileState') in allowed, 'run guest.py cleanup before updating')
    uid, _ = account()
    names = {'scenarios.py', 'guest.py', 'synthetic.py', 'daily_backup.py', 'backup_restore.py', 'offsite_backup.py'}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit() or int(directory.name) == os.getpid():
            continue
        try:
            status = (directory / 'status').read_text()
            args = (directory / 'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            continue
        process_uid = int(next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1])
        guard.require(process_uid != uid and not any(os.fsdecode(arg).rsplit('/', 1)[-1] in names for arg in args),
                      'test process still running')


def attestation():
    data, _ = read_file(ROOT / 'guest.json')
    record = json.loads(data)
    expected = {f'/etc/systemd/system/{u}' for u in guard.UNITS} | {'/etc/sudoers.d/tdb-harness'}
    guard.require(set(record['installed']) == expected, 'unknown installed system resources')
    for path, sha in record['installed'].items():
        guard.require(digest(read_file(Path(path))[0]) == sha, 'installed unit/sudoers changed')
    return digest(data), record


def payload(directory):
    manifest = json.loads(read_file(directory / 'bundle.json')[0])
    guard.require(manifest.get('base') == release.BASE and manifest.get('old_base') == release.OLD_BASE
                  and manifest.get('old_harness') == release.OLD_HARNESS
                  and re.fullmatch('[0-9a-f]{40}', manifest.get('harness', '')),
                  'wrong source/harness SHA')
    guard.require(set(manifest['sha256']) == {*release.SOURCE, *release.HARNESS}, 'unknown payload files')
    for name, sha in manifest['sha256'].items():
        data = read_file(directory / name)[0]
        guard.require(digest(data) == sha, 'payload checksum mismatch')
        if name in release.SOURCE_HASHES:
            guard.require(sha == release.SOURCE_HASHES[name], 'source does not match reviewed commit')
    return manifest


def journal():
    pointer = ROOT / 'update-current.json'
    if not pointer.exists():
        guard.require(not pointer.is_symlink(), 'unsafe update pointer')
        return None, None
    txid = json.loads(read_file(pointer)[0])['id']
    guard.require(re.fullmatch('[0-9a-f]{32}', txid), 'invalid transaction ID')
    directory = ROOT / 'updates' / txid
    record = json.loads(read_file(directory / 'journal.json')[0])
    guard.require(record['id'] == txid and record['base'] == release.BASE
                  and set(record['files']) == {*controller_paths(), 'version.json'}, 'invalid update journal')
    guard.require(record['state'] in ('applying', 'committed', 'rolled_back'), 'unknown transaction state')
    for name, info in record['files'].items():
        guard.require(type(info['index']) is int and 0 <= info['index'] < len(record['files'])
                      and info['meta'] == expected_attributes(name)
                      and re.fullmatch('[0-9a-f]{64}', info['new'])
                      and (info['old'] is None or re.fullmatch('[0-9a-f]{64}', info['old'])),
                      'invalid transaction file metadata')
    guard.require(len({v['index'] for v in record['files'].values()}) == len(record['files']), 'duplicate file index')
    return directory, record


def verify(*, _applying=False):
    root_check()
    check_directories()
    _, transaction = journal()
    guard.require(transaction is None or transaction['state'] == 'committed' or
                  (_applying and transaction['state'] == 'applying'), 'update incomplete: run recover')
    data, meta = read_file(ROOT / 'version.json')
    guard.require(meta == expected_attributes('version.json'), 'unsafe version receipt')
    version = json.loads(data)
    if transaction is not None:
        guard.require(digest(read_file(ROOT / 'version.json')[0]) == transaction['files']['version.json']['new'],
                      'version receipt changed')
    attested, _ = attestation()
    guard.require(version['base'] == release.BASE and version['attestation_sha256'] == attested,
                  'installed version/attestation mismatch')
    guard.require(set(version['files']) == {*controller_paths(), *wrappers()}, 'unknown version inventory')
    for name, sha in version['files'].items():
        data, meta = read_file(ROOT / name)
        guard.require(digest(data) == sha and meta == expected_attributes(name), 'installed file changed: ' + name)
    for target, source in controller_paths().items():
        if source in release.SOURCE_HASHES:
            guard.require(version['files'][target] == release.SOURCE_HASHES[source], 'installed source SHA mismatch')
    for name, content in wrappers().items():
        guard.require(version['files'][name] == digest(content), 'synthetic wrapper changed')
    return version


def version_record(manifest, origin, attested):
    return dict(base=release.BASE, harness=manifest['harness'], origin=origin, attestation_sha256=attested,
                files={target: manifest['sha256'][name] for target, name in controller_paths().items()} |
                      {name: digest(content) for name, content in wrappers().items()})


def register_provision(manifest):
    root_check()
    guard.require(not (ROOT / 'version.json').exists(), 'version already registered')
    attested, original = attestation()
    guard.require(original['base'] == release.BASE, 'provision version does not match attestation')
    version = version_record(manifest, 'provision', attested)
    atomic(ROOT / 'version.json', json.dumps(version).encode(), expected_attributes('version.json'))
    verify()


def apply(directory):
    root_check()
    manifest = payload(directory)
    _, previous = journal()
    guard.require(previous is None or previous['state'] == 'rolled_back', 'update exists: verify or recover first')
    check_units()
    check_directories()
    attested, record = attestation()
    guard.require(record['base'] == release.OLD_BASE, 'not the supported original installation')
    # Explicit inventory: refuse unknown files in code/admin, including hidden files.
    allowed_code = set(release.OLD_HASHES)
    actual_code = set()
    for path in (ROOT / 'code').rglob('*'):
        guard.no_links(path)
        name = path.relative_to(ROOT / 'code').as_posix()
        if path.is_dir():
            guard.require(name in ('tools', 'src'), 'unknown installed code directory')
        else:
            actual_code.add(name)
    guard.require(actual_code == allowed_code, 'unknown/missing installed code')
    guard.require({p.name for p in (ROOT / 'admin').iterdir()} <=
                  {'daily_backup.py', 'backup_restore.py', 'offsite_backup.py', 'daily.json'}, 'unknown admin files')
    for path in (ROOT / 'admin').iterdir():
        guard.no_links(path)
    old = {}
    for target, source in controller_paths().items():
        path = ROOT / target
        if source in ('release.py', 'update.py'):
            guard.require(not path.exists() and not path.is_symlink(), 'new module already exists')
            old[target] = None
            continue
        data, meta = read_file(path)
        guard.require(digest(data) in release.OLD_HASHES[source] and meta == expected_attributes(target),
                      'modified original file: ' + target)
        old[target] = data
    for name, content in wrappers().items():
        data, meta = read_file(ROOT / name)
        guard.require(data == content and meta == expected_attributes(name), 'modified synthetic wrapper')
    guard.require(not (ROOT / 'version.json').exists() and not (ROOT / 'version.json').is_symlink(), 'version collision')
    old['version.json'] = None
    txid = uuid.uuid4().hex
    transactions = ROOT / 'updates'
    guard.no_links(transactions)
    transactions.mkdir(mode=0o700, exist_ok=True)
    tx = transactions / txid
    tx.mkdir(mode=0o700)
    sync_dir(ROOT)
    sync_dir(transactions)
    # Recovery remains available without a mounted ISO, even after a partial rollback.
    for name in ('guard.py', 'release.py', 'update.py'):
        atomic(tx / name, read_file(directory / name)[0], dict(mode=0o600, uid=0, gid=0))
    new = {target: read_file(directory / name)[0] for target, name in controller_paths().items()}
    new['version.json'] = json.dumps(version_record(manifest, 'upgrade', attested), sort_keys=True).encode()
    files = {}
    for index, (name, data) in enumerate(new.items()):
        before = old[name]
        files[name] = dict(index=index, old=digest(before) if before is not None else None,
                           new=digest(data), meta=expected_attributes(name))
        atomic(tx / f'{index}.new', data, dict(mode=0o600, uid=0, gid=0))
        if before is not None:
            atomic(tx / f'{index}.old', before, dict(mode=0o600, uid=0, gid=0))
    transaction = dict(id=txid, base=release.BASE, state='applying', attestation_sha256=attested, files=files)
    write_json(tx / 'journal.json', transaction)
    write_json(ROOT / 'update-current.json', dict(id=txid))
    publish_status(transaction)
    try:
        with staging(tx):
            return finish_apply(tx, transaction, attested)
    except BaseException:
        recover()
        raise


def finish_apply(tx, transaction, attested):
    files = transaction['files']
    order = ['code/guard.py', *[name for name in files if name != 'code/guard.py']]
    for name in order:
        info = files[name]
        path = ROOT / name
        guard.no_links(path)
        current = None
        if path.exists():
            content, meta = read_file(path)
            guard.require(meta == info['meta'], 'file attributes changed before replacement')
            current = digest(content)
        guard.require(current == info['old'], 'file changed before replacement')
        atomic(path, read_file(tx / f'{info["index"]}.new')[0], info['meta'])
    # Verify without accepting an applying journal as a completed migration.
    for name, info in files.items():
        data, meta = read_file(ROOT / name)
        guard.require(digest(data) == info['new'] and meta == info['meta'], 'incomplete replacement')
    guard.require(attestation()[0] == attested, 'original attestation changed')
    version = verify(_applying=True)
    transaction['state'] = 'committed'
    write_json(tx / 'journal.json', transaction)
    publish_status(transaction)
    return version

def recover():
    root_check()
    check_units()
    check_directories()
    tx, transaction = journal()
    guard.require(transaction is not None and transaction['state'] in ('applying', 'rolled_back'),
                  'no interrupted update to recover')
    guard.require(attestation()[0] == transaction['attestation_sha256'], 'original attestation changed')
    with staging(tx):
        restore_files(tx, transaction)
    transaction['state'] = 'rolled_back'
    write_json(tx / 'journal.json', transaction)
    publish_status(transaction)
    print('ROLLBACK_OK: original code restored; cases/reports/attestation retained')


def restore_files(tx, transaction):
    # The new guard blocks normal commands until all older code is restored.
    # Restore that guard last, including when recovery itself was interrupted.
    order = sorted(transaction['files'], key=lambda name: name == 'code/guard.py')
    for name in order:
        info = transaction['files'][name]
        path = ROOT / name
        guard.no_links(path)
        current = digest(read_file(path)[0]) if path.exists() else None
        guard.require(current in (info['old'], info['new']), 'unknown file during recovery: ' + name)
        if current == info['old']:
            if path.exists():
                guard.require(read_file(path)[1] == info['meta'], 'original attributes changed during recovery')
            continue
        if info['old'] is None:
            if path.exists():
                path.unlink()  # Only the exact new file owned by this transaction.
                sync_dir(path.parent)
        else:
            data = read_file(tx / f'{info["index"]}.old')[0]
            guard.require(digest(data) == info['old'], 'damaged recovery copy')
            atomic(path, data, info['meta'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('apply', 'verify', 'recover'))
    args = parser.parse_args()
    with lock():
        if args.action == 'apply':
            version = apply(Path(__file__).resolve().parent)
        elif args.action == 'recover':
            recover()
            return
        else:
            version = verify()
            _, transaction = journal()
            if transaction is not None:
                publish_status(transaction)  # Finish a committed receipt after a lost final fsync.
        print('UPDATE_VERIFIED base=' + version['base'] + ' harness=' + version['harness'])


if __name__ == '__main__':
    try:
        main()
    except (guard.Refused, OSError, ValueError, KeyError) as error:
        sys.exit(f'REFUSED/FAILED: {error}')
