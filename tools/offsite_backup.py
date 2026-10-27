"""Send a sealed TDB snapshot to Dropbox. No bot, database writes or private age keys."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import threading
import time
import uuid

if __package__:
    from . import backup_restore as snapshots
else:
    # Also works under python -I -S: load only the reviewed sibling module.
    spec = importlib.util.spec_from_file_location("tdb_backup_restore", Path(__file__).with_name("backup_restore.py"))
    snapshots = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshots)

RECIPIENT = "age1afe9mj8gqgvtxm0e9cla8e2pmcj238nthcrfr5drmv45c29qd5as3kshj3"
REMOTE = "tdb-dropbox:TCD-Backups"
JOB_FORMAT = "tdb-offsite-state-v1"  # Local workflow journal, never a new snapshot format.
NAME = r"tdb-v1-[0-9]{8}T[0-9]{6}Z-([0-9a-f]{32})\.tar\.gz\.age"
STATES = {"packing", "prepared", "uploading", "staged", "publishing", "verified", "deleting", "pruned"}
META_LIMIT = 2 * 1024 * 1024
MAX_JOBS = 1000
BLOCK = 512 * 1024


class OffsiteError(RuntimeError):
    pass


class Limits:
    def __init__(self, max_archives=3, budget_bytes=1500000000, account_reserve=300000000,
                 local_reserve=1024**3, timeout=1800, go_memory_mib=96):
        for value in (max_archives, budget_bytes, account_reserve, local_reserve, go_memory_mib):
            if type(value) is not int or value < 1:
                raise OffsiteError("Limits must be positive integers")
        if max_archives > MAX_JOBS or not 32 <= go_memory_mib <= 256 or not math.isfinite(timeout) or timeout <= 0:
            raise OffsiteError("Invalid count, memory budget or timeout")
        self.max_archives, self.budget_bytes = max_archives, budget_bytes
        self.account_reserve, self.local_reserve = account_reserve, local_reserve
        self.timeout, self.go_memory_mib = timeout, go_memory_mib


def _private(path, create=False):
    snapshots._no_links(path)
    if create and not path.exists():
        snapshots._mkdir(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OffsiteError("Expected a private directory")
    if os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise OffsiteError("State directories must be owned by this user with mode 0700")


def _private_file(path):
    snapshots._no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OffsiteError("State or ciphertext is not a private regular file")
    if os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise OffsiteError("State and ciphertext files require owner-only permissions")
    return info


@contextmanager
def _lease(directory):
    _private(directory, create=True)
    path = directory/'offsite.lock'
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0)
    if os.name == 'posix':
        with snapshots._open_input(directory, directory=True) as parent:
            fd = os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    else:
        snapshots._no_links(path)
        fd = os.open(path, flags, 0o600)
    try:
        before = _private_file(path)
        if (before.st_dev, before.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
            raise OffsiteError("Lock file was replaced")
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            raise OffsiteError("Another offsite operation owns this state directory") from None
        yield fd
    finally:
        os.close(fd)  # Never unlink the lock: a new inode would defeat mutual exclusion.


def _remote(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}:(?:[A-Za-z0-9][A-Za-z0-9._-]{0,79})(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,79})*", value):
        raise OffsiteError("Use a named rclone remote and a non-root folder without traversal")
    return value


class External:
    """Bounded output, one command at a time, redacted failures and reaped children."""
    def __init__(self, state_dir, limits, cancel=None, age='age', rclone='rclone'):
        self.directory, self.limits = state_dir, limits
        self.deadline = time.monotonic() + limits.timeout
        self.cancel = cancel or threading.Event()
        self.age, self.rclone = age, rclone
        self.lock_fd = None
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('RCLONE_') or k in ('RCLONE_CONFIG', 'RCLONE_CONFIG_PASS')}
        self.env.update(GOMAXPROCS='1', GOGC='50', GOMEMLIMIT=f'{limits.go_memory_mib}MiB')

    def check(self, extra=0):
        if self.cancel.is_set():
            raise OffsiteError("Operation interrupted")
        if time.monotonic() >= self.deadline:
            raise OffsiteError("Offsite operation timed out")
        if shutil.disk_usage(self.directory).free < self.limits.local_reserve + extra:
            raise OffsiteError("Insufficient local space above reserve")

    def remaining(self):
        self.check()
        return max(0.001, self.deadline - time.monotonic())

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == 'posix':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
        process.wait()

    @contextmanager
    def process(self, argv, *, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, monitor=None):
        self.check()
        options = dict(stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL, env=self.env,
                       start_new_session=os.name == 'posix')
        if os.name == 'posix' and self.lock_fd is not None:
            options['pass_fds'] = (self.lock_fd,)
        process = subprocess.Popen(argv, **options)  # argv list only; never shell=True.
        finished, failures = threading.Event(), []

        def watch():
            while not finished.wait(0.05):
                try:
                    self.check()
                    if monitor:
                        monitor()
                except (OffsiteError, OSError):
                    failures.append(OffsiteError("External operation timed out, interrupted or exhausted local space"))
                    self._stop(process)
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            try:
                yield process
            except BaseException:
                if failures:
                    raise failures[0] from None
                raise
            status = process.wait()
            if failures:
                raise failures[0]
            if status != 0:
                raise OffsiteError("External command failed; raw output is suppressed")
            self.check()
            if monitor:
                monitor()
        finally:
            finished.set()
            self._stop(process)
            watcher.join()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def run(self, argv):
        chunks, size = [], 0
        with self.process(argv) as process:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > META_LIMIT:
                    raise OffsiteError("External metadata exceeds the supported limit")
                chunks.append(chunk)
        return b''.join(chunks)

    def rc(self, *args, deleting=False):
        return self.run([self.rclone, *args, '--transfers', '1', '--checkers', '1',
            '--buffer-size', '1Mi', '--multi-thread-streams', '0', '--dropbox-chunk-size', '4Mi',
            '--dropbox-batch-mode', 'off', '--dropbox-shared-files=false', '--dropbox-shared-folders=false',
            '--retries', '1', '--low-level-retries', '2',
            '--contimeout', '15s', '--timeout', '60s', '--stats', '0', '--log-level', 'ERROR',
            '--max-delete', '1' if deleting else '0'])

    def rc_json(self, *args):
        try:
            return json.loads(self.rc(*args))
        except (ValueError, UnicodeError, RecursionError):
            raise OffsiteError("Invalid rclone JSON; no remote mutation authorized") from None


def _versions(external, remote):
    version = external.run([external.rclone, 'version']).decode('ascii', errors='replace')
    found = re.search(r'^rclone v(\d+)\.(\d+)\.(\d+)', version)
    if not found or tuple(map(int, found.groups())) < (1, 60, 1):
        raise OffsiteError("rclone 1.60.1 or later is required")
    version = external.run([external.age, '--version']).decode('ascii', errors='replace').strip()
    found = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
    if not found or tuple(map(int, found.groups())) < (1, 1, 1):
        raise OffsiteError("age 1.1.1 or later is required")
    features = external.rc_json('backend', 'features', remote, '--json')
    name, root = remote.split(':', 1)
    if (not isinstance(features, dict) or features.get('Name') != name or features.get('Root') != root
            or features.get('String') != f"Dropbox root '{root}'" or features.get('Hashes') != ['dropbox']
            or not isinstance(features.get('Features'), dict)
            or any(features['Features'].get(key) is not True for key in ('Move','About','CaseInsensitive'))):
        raise OffsiteError("A direct Dropbox backend with quota and server-side Move is required")


def _integer(value):
    return type(value) is int and value >= 0


def _admission(external, remote, additional):
    quota = external.rc_json('about', remote.split(':', 1)[0]+':', '--json')
    usage = external.rc_json('size', remote, '--json')
    if (not isinstance(quota, dict) or not _integer(quota.get('free'))
            or not isinstance(usage, dict) or not _integer(usage.get('bytes'))
            or not _integer(usage.get('count')) or usage.get('sizeless') != 0):
        raise OffsiteError("Quota or TCD folder size is unknown")
    if quota['free'] < external.limits.account_reserve + additional:
        raise OffsiteError("Dropbox account reserve would be breached; no preliminary deletion")
    if usage['bytes'] + additional > external.limits.budget_bytes:
        raise OffsiteError("TCD budget would be breached; no preliminary deletion")


def _listing(external, remote):
    items = external.rc_json('lsjson', remote, '--hash', '--no-mimetype')
    if not isinstance(items, list) or len(items) > 10000:
        raise OffsiteError("Invalid or oversized Dropbox listing")
    found = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('Path'), str):
            raise OffsiteError("Invalid Dropbox entry")
        key = item['Path'].casefold()
        if key in found:
            raise OffsiteError("Ambiguous case-insensitive Dropbox name")
        found[key] = item
    return found


def _identity(item):
    if (not isinstance(item, dict) or item.get('IsDir') is not False or not _integer(item.get('Size'))
            or not isinstance(item.get('ID'), str) or not re.fullmatch(r'id:[A-Za-z0-9_-]{1,200}', item['ID'])
            or not isinstance(item.get('Hashes'), dict)
            or not isinstance(item['Hashes'].get('dropbox'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', item['Hashes'].get('dropbox', ''))
            or not isinstance(item.get('ModTime'), str) or not re.fullmatch(r'[0-9T:Z+.-]{1,64}', item['ModTime'])):
        raise OffsiteError("Dropbox object lacks sufficient ownership evidence")
    return dict(id=item['ID'], size=item['Size'], dropbox_hash=item['Hashes']['dropbox'], modtime=item['ModTime'])


def _stat_remote(external, remote, name):
    return _identity(external.rc_json('lsjson', remote+'/'+name, '--stat', '--hash', '--no-mimetype'))


def _hash_cipher(path, external):
    before = snapshots._stamp(_private_file(path))
    full, combined, block, block_size, count = hashlib.sha256(), hashlib.sha256(), hashlib.sha256(), 0, 0
    with snapshots._open_input(path) as fd:
        if snapshots._stamp(os.fstat(fd)) != before:
            raise OffsiteError("Ciphertext was replaced")
        while True:
            external.check()
            chunk = os.read(fd, min(BLOCK, 4*1024**2-block_size))
            if not chunk:
                break
            full.update(chunk)
            block.update(chunk)
            count += len(chunk)
            block_size += len(chunk)
            if block_size == 4*1024**2:
                combined.update(block.digest())
                block, block_size = hashlib.sha256(), 0
        if block_size:
            combined.update(block.digest())
        if snapshots._stamp(os.fstat(fd)) != before or snapshots._stamp(path.lstat()) != before or count != before[2]:
            raise OffsiteError("Ciphertext changed while hashing")
    return dict(size=count, sha256=full.hexdigest(), dropbox_hash=combined.hexdigest(), identity=before)


def _save(job_dir, job, external):
    _private(job_dir)
    content = json.dumps(job, sort_keys=True).encode('utf-8')
    if len(content) > 8192:
        raise OffsiteError("Workflow journal is too large")
    target = job_dir/'job.json'
    if target.exists() or target.is_symlink():
        _private_file(target)
    temporary = job_dir/('.state-'+uuid.uuid4().hex+'.tmp')
    snapshots._write(temporary, content, external)
    if os.name == 'posix':
        with snapshots._open_input(job_dir, directory=True) as fd:
            os.replace(temporary.name, target.name, src_dir_fd=fd, dst_dir_fd=fd)
    else:
        snapshots._no_links(job_dir)
        os.replace(temporary, target)
    snapshots._sync_directory(job_dir)


def _load(job_dir):
    _private(job_dir)
    _private_file(job_dir/'job.json')
    try:
        job = json.loads(snapshots._read_small(job_dir/'job.json', 8192), object_pairs_hook=snapshots._unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise OffsiteError("Invalid private workflow journal") from None
    if (not isinstance(job, dict) or set(job) != {'format','id','name','remote','manifest_sha256',
            'recipient','state','cipher','stage_identity','remote_identity'}
            or job['format'] != JOB_FORMAT or job['id'] != job_dir.name
            or not re.fullmatch(r'[0-9a-f]{32}', job_dir.name)
            or not isinstance(job['name'], str) or not re.fullmatch(NAME, job['name'])
            or re.fullmatch(NAME, job['name']).group(1) != job['id']
            or not isinstance(job['state'], str) or job['state'] not in STATES
            or not isinstance(job['manifest_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', job['manifest_sha256'])):
        raise OffsiteError("Unrecognized workflow journal; ownership will not be inferred from names")
    _remote(job['remote'])
    _recipient(job['recipient'])
    if job['state'] != 'packing':
        cipher = job['cipher']
        if (not isinstance(cipher, dict) or set(cipher) != {'size','sha256','dropbox_hash','identity'}
                or not _integer(cipher['size']) or cipher['size'] == 0
                or any(not isinstance(cipher.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', cipher[key])
                       for key in ('sha256','dropbox_hash'))
                or not isinstance(cipher['identity'], list) or len(cipher['identity']) != 5
                or any(type(n) is not int for n in cipher['identity'])):
            raise OffsiteError("Invalid local ciphertext proof")
    for key in ('stage_identity','remote_identity'):
        value = job[key]
        if value is not None:
            if (not isinstance(value, dict) or set(value) != {'id','size','dropbox_hash','modtime'}
                    or not isinstance(value['id'], str) or not re.fullmatch(r'id:[A-Za-z0-9_-]{1,200}', value['id'])
                    or not _integer(value['size']) or not isinstance(value['dropbox_hash'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', value['dropbox_hash']) or not isinstance(value['modtime'], str)):
                raise OffsiteError("Invalid remote ownership proof")
    if job['state'] in ('staged','publishing') and job['stage_identity'] is None:
        raise OffsiteError("Missing staging identity")
    if job['state'] in ('verified','deleting','pruned') and job['remote_identity'] is None:
        raise OffsiteError("Missing verified identity")
    return job


def _jobs(directory):
    records = []
    for name, info in snapshots._entries(directory):
        if not re.fullmatch(r'[0-9a-f]{32}', name) or not stat.S_ISDIR(info.st_mode):
            raise OffsiteError("Unexpected entry in the private jobs directory")
        records.append((directory/name, _load(directory/name)))
        if len(records) > MAX_JOBS:
            raise OffsiteError("Too many local jobs; retain journals and review retention manually")
    return records


def _recipient(value):
    if not isinstance(value, str) or not re.fullmatch(r'age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}', value):
        raise OffsiteError("An age X25519 public recipient is required; never supply a private key")
    return value


def _pack(source, job_dir, job, manifest, external):
    members = [(e['path'], e['size'], e['sha256']) for e in manifest['files']]
    for name, limit in (('manifest.json', snapshots.MAX_MANIFEST), ('COMPLETE', 65)):
        data = snapshots._read_small(source/name, limit)
        members.append((name, len(data), hashlib.sha256(data).hexdigest()))
    upper_bound = 2*(sum(size for _, size, _ in members) + 4096*(len(members)+len(manifest['directories']))) + 1024**2
    external.check(min(upper_bound, external.limits.budget_bytes + 1024**2))
    payload = job_dir/'payload'
    snapshots._mkdir(payload)
    path = payload/job['name']
    with snapshots._new_file(path) as output:
        def monitor():
            if os.fstat(output.fileno()).st_size > external.limits.budget_bytes:
                raise OffsiteError("Encrypted archive exceeds the TCD budget")
        with external.process([external.age, '--encrypt', '--recipient', job['recipient']],
                              stdin=subprocess.PIPE, stdout=output, monitor=monitor) as process:
            with tarfile.open(fileobj=process.stdin, mode='w|gz', bufsize=65536) as archive:
                for relative in ['', *[e['path'] for e in manifest['directories']]]:
                    info = tarfile.TarInfo('snapshot' + ('/'+relative if relative else ''))
                    info.type, info.mode = tarfile.DIRTYPE, 0o700
                    archive.addfile(info)
                for relative, size, expected_hash in members:
                    external.check()
                    with snapshots._open_input(source/relative) as fd:
                        before = snapshots._stamp(os.fstat(fd))
                        digest, total = hashlib.sha256(), [0]
                        class Reader:
                            def read(self, count):
                                external.check()
                                chunk = os.read(fd, min(count, BLOCK))
                                if len(chunk) != min(count, BLOCK):
                                    raise OffsiteError("Snapshot became shorter while archiving")
                                digest.update(chunk)
                                total[0] += len(chunk)
                                return chunk
                        info = tarfile.TarInfo('snapshot/'+relative)
                        info.size, info.mode = size, 0o600
                        archive.addfile(info, Reader())
                        if (os.read(fd, 1) or total[0] != size or digest.hexdigest() != expected_hash
                                or snapshots._stamp(os.fstat(fd)) != before
                                or snapshots._stamp((source/relative).lstat()) != before):
                            raise OffsiteError("Snapshot changed while archiving")
            process.stdin.close()
    if snapshots.verify_backup(source, timeout=external.remaining()) != manifest:
        raise OffsiteError("Snapshot changed during encryption")
    job['cipher'] = _hash_cipher(path, external)
    if job['cipher']['size'] == 0 or job['cipher']['size'] > external.limits.budget_bytes:
        raise OffsiteError("Encrypted archive is empty or exceeds the TCD budget")
    job['state'] = 'prepared'
    _save(job_dir, job, external)


def _matches_cipher(identity, job):
    return identity['size'] == job['cipher']['size'] and identity['dropbox_hash'] == job['cipher']['dropbox_hash']


def _local(job_dir, job, external):
    path = job_dir/'payload'/job['name']
    if _hash_cipher(path, external) != job['cipher']:
        raise OffsiteError("Local ciphertext no longer matches the private journal")
    return path


def _verify_cloud(job_dir, job, external):
    _local(job_dir, job, external)
    before = _stat_remote(external, job['remote'], job['name'])
    expected = job['remote_identity'] or job['stage_identity']
    if not expected or before != expected or not _matches_cipher(before, job):
        raise OffsiteError("Published object identity changed; nothing will be deleted")
    report = external.rc('check', str(job_dir/'payload'), job['remote'], '--download', '--one-way',
                         '--include', '/'+job['name'], '--combined', '-')
    if report != ('= '+job['name']+'\n').encode('ascii'):
        raise OffsiteError("Full download verification did not confirm exactly one archive")
    _local(job_dir, job, external)
    if _stat_remote(external, job['remote'], job['name']) != before:
        raise OffsiteError("Dropbox object changed during download verification")
    job.update(state='verified', remote_identity=before)
    _save(job_dir, job, external)


def _publish(job_dir, job, external):
    path = _local(job_dir, job, external)
    remote, name = job['remote'], job['name']
    temporary = '_tdb-upload-'+job['id']+'.age'
    listing = _listing(external, remote)
    final, staged = listing.get(name.casefold()), listing.get(temporary.casefold())
    if final is not None:
        if job['state'] not in ('publishing','verified'):
            raise OffsiteError("Final name already exists; overwrite is forbidden")
        _verify_cloud(job_dir, job, external)
        return
    if job['state'] == 'verified':
        raise OffsiteError("Previously verified cloud archive is missing")
    if staged is None:
        if job['state'] != 'prepared':
            raise OffsiteError("Uncertain upload/publish outcome; preserve the journal for review")
        _admission(external, remote, job['cipher']['size'])
        external.rc('copyto', str(path), remote+'/'+temporary, '--ignore-existing', '--immutable', '--dry-run')
        listing = _listing(external, remote)
        if temporary.casefold() in listing or name.casefold() in listing:
            raise OffsiteError("Remote name collision; no upload authorized")
        job['state'] = 'uploading'
        _save(job_dir, job, external)
        external.rc('copyto', str(path), remote+'/'+temporary, '--ignore-existing', '--immutable')
        staged_identity = _stat_remote(external, remote, temporary)
    else:
        if job['state'] not in ('uploading','staged','publishing'):
            raise OffsiteError("Unowned staging name collision")
        staged_identity = _identity(staged)
    if not _matches_cipher(staged_identity, job) or (job['stage_identity'] and staged_identity != job['stage_identity']):
        raise OffsiteError("Staging object differs; no overwrite, deletion or adoption authorized")
    job.update(state='staged', stage_identity=staged_identity)
    _save(job_dir, job, external)
    if name.casefold() in _listing(external, remote):
        raise OffsiteError("Final name collision; existing file retained")
    job['state'] = 'publishing'
    _save(job_dir, job, external)
    # ignore-existing prevents rclone's pre-move deletion; Dropbox MoveV2 itself
    # fails on a destination collision. max-delete=0 also forbids CLI deletions.
    external.rc('moveto', remote+'/'+temporary, remote+'/'+name, '--ignore-existing', '--immutable')
    _verify_cloud(job_dir, job, external)


def _rotate(jobs, job_dir, job, external):
    # _publish just completed the full download check, including on resume.
    listing = _listing(external, job['remote'])
    owned = []
    for directory, record in _jobs(jobs):
        if record['remote'] != job['remote'] or record['state'] not in ('verified','deleting'):
            continue
        item = listing.get(record['name'].casefold())
        if item is None:
            record['state'] = 'pruned'
            _save(directory, record, external)
            continue
        if _identity(item) != record['remote_identity'] or not _matches_cipher(record['remote_identity'], record):
            raise OffsiteError("An owned cloud object was replaced; rotation refused")
        owned.append((directory, record))
    owned.sort(key=lambda pair: pair[1]['name'])
    victims = [pair for pair in owned if pair[1]['id'] != job['id']][:max(0, len(owned)-external.limits.max_archives)]
    for directory, old in victims:
        if _stat_remote(external, job['remote'], job['name']) != job['remote_identity']:
            raise OffsiteError("New verified copy changed; rotation stopped")
        if _stat_remote(external, old['remote'], old['name']) != old['remote_identity']:
            raise OffsiteError("Rotation candidate changed; it is retained")
        old['state'] = 'deleting'
        _save(directory, old, external)
        external.rc('deletefile', old['remote']+'/'+old['name'], deleting=True)
        if old['name'].casefold() in _listing(external, old['remote']):
            raise OffsiteError("Deletion was not confirmed; new verified copy is retained")
        old['state'] = 'pruned'
        _save(directory, old, external)
    final = _listing(external, job['remote'])
    if _identity(final.get(job['name'].casefold())) != job['remote_identity']:
        raise OffsiteError("The retained new copy is missing or changed")
    remaining = [record for _, record in _jobs(jobs) if record['remote'] == job['remote']
                 and record['state'] in ('verified','deleting') and record['name'].casefold() in final]
    if len(remaining) > external.limits.max_archives:
        raise OffsiteError("Managed archive count remains above limit")
    _admission(external, job['remote'], 0)


def send_backup(source, state_dir, *, remote=REMOTE, recipient=RECIPIENT, limits=None,
                resume=None, external=None, cancel=None):
    source, state_dir = snapshots._absolute(source), snapshots._absolute(state_dir)
    remote, recipient = _remote(remote), _recipient(recipient)
    limits = limits or Limits()
    if snapshots._overlap(source, state_dir):
        raise OffsiteError("State directory must be separate from the sealed backup")
    deadline = time.monotonic() + limits.timeout
    manifest = snapshots.verify_backup(source, timeout=limits.timeout)
    fingerprint = snapshots._read_small(source/'COMPLETE', 65).decode('ascii').strip()
    if snapshots._manifest(source) != manifest:
        raise OffsiteError("Snapshot manifest changed")
    for original in [manifest['source_database'], *manifest['roots']]:
        path = Path(original)
        if path.is_absolute() and snapshots._overlap(state_dir, path):
            raise OffsiteError("State directory overlaps original production data")
    with _lease(state_dir) as lock_fd:
        external = external or External(state_dir, limits, cancel=cancel)
        external.lock_fd = lock_fd
        external.deadline = min(external.deadline, deadline)
        external.check()
        jobs = state_dir/'jobs'
        _private(jobs, create=True)
        records = _jobs(jobs)
        _versions(external, remote)
        _admission(external, remote, 0)
        if resume is not None:
            if not re.fullmatch(r'[0-9a-f]{32}', resume):
                raise OffsiteError("Invalid resume ID")
            job_dir = jobs/resume
            job = _load(job_dir)
            if (job['remote'], job['recipient'], job['manifest_sha256']) != (remote, recipient, fingerprint):
                raise OffsiteError("Resume parameters do not match the recorded operation")
            if job['state'] in ('packing','deleting','pruned'):
                raise OffsiteError("This job cannot be resumed as a send; preserve it for review")
        else:
            if len(records) >= MAX_JOBS:
                raise OffsiteError("Local journal count limit reached")
            if any(r['remote'] == remote and r['state'] in ('prepared','uploading','staged','publishing') for _, r in records):
                raise OffsiteError("An unfinished send exists; use its private journal ID with --resume")
            identifier = uuid.uuid4().hex
            name = 'tdb-v1-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+identifier+'.tar.gz.age'
            job_dir = jobs/identifier
            snapshots._mkdir(job_dir)
            job = dict(format=JOB_FORMAT, id=identifier, name=name, remote=remote,
                       manifest_sha256=fingerprint, recipient=recipient, state='packing', cipher=None,
                       stage_identity=None, remote_identity=None)
            _save(job_dir, job, external)
            _pack(source, job_dir, job, manifest, external)
        try:
            _publish(job_dir, job, external)
            _rotate(jobs, job_dir, job, external)
        except (OffsiteError, OSError, snapshots.BackupError):
            raise OffsiteError("Send or rotation did not complete; inspect private job "+job['id']+" and resume explicitly") from None
        return dict(status='ok', id=job['id'], archive=job['name'], sha256=job['cipher']['sha256'],
                    bytes=job['cipher']['size'], cloud_verified=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--remote', default=REMOTE)
    parser.add_argument('--recipient', default=RECIPIENT)
    parser.add_argument('--resume')
    parser.add_argument('--max-archives', type=int, default=3)
    parser.add_argument('--budget-bytes', type=int, default=1500000000)
    parser.add_argument('--account-reserve-bytes', type=int, default=300000000)
    parser.add_argument('--local-reserve-bytes', type=int, default=1024**3)
    parser.add_argument('--timeout-seconds', type=float, default=1800)
    parser.add_argument('--go-memory-mib', type=int, default=96)
    args = parser.parse_args(argv)
    cancel, previous = threading.Event(), {}
    for name in ('SIGINT','SIGTERM'):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, lambda *_: cancel.set())
    try:
        limits = Limits(args.max_archives, args.budget_bytes, args.account_reserve_bytes,
                        args.local_reserve_bytes, args.timeout_seconds, args.go_memory_mib)
        result = send_backup(args.backup, args.state_dir, remote=args.remote, recipient=args.recipient,
                             limits=limits, resume=args.resume, cancel=cancel)
    except (OffsiteError, snapshots.BackupError, OSError, ValueError, tarfile.TarError) as error:
        message = str(error) if isinstance(error, OffsiteError) else 'Local input, I/O or snapshot verification failed'
        parser.exit(1, 'ERROR: '+message+'. Raw external output is suppressed; no broad cleanup is performed.\n')
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
