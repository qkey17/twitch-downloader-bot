"""One operation: verified Ubuntu VM, real systemd suite, VM power loss, artifacts."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host
import iso
import release
import wire

IMAGE_URL = ('https://cloud-images.ubuntu.com/releases/noble/release-20260801/'
             'ubuntu-24.04-server-cloudimg-amd64.img')
IMAGE_SHA256 = '0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe'

# Seed-owned bootstrap runs in a NEW image, before the normal root-label guard
# can pass. It proves this is the offline QEMU guest and its known virtio root
# device before labeling it. No host mount, shell interpolation, or package IO.
BOOTSTRAP = r'''
import hashlib, json, os, socket, stat, struct, subprocess, sys
from pathlib import Path
config = json.loads(Path('/run/tdb-auto-config.json').read_text())
assert Path('/sys/class/dmi/id/sys_vendor').read_text().strip() == 'TDB-Harness'
assert Path('/sys/class/dmi/id/product_name').read_text().strip() == 'TDB-Isolated-VM'
assert Path('/proc/1/comm').read_text().strip() == 'systemd'
assert os.readlink('/proc/self/ns/pid') == os.readlink('/proc/1/ns/pid')
assert sorted(p.name for p in Path('/sys/class/net').iterdir()) == ['lo']
assert not Path('/.dockerenv').exists() and not Path('/run/systemd/container').exists()
mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
assert not any(m[m.index('-')+1] in ('9p','virtiofs','nfs','nfs4','cifs','fuse.sshfs') for m in mounts)
root = next(m for m in mounts if m[4] == '/')
device = Path('/dev/vda1').stat()
assert stat.S_ISBLK(device.st_mode)
assert root[2] == str(os.major(device.st_rdev))+':'+str(os.minor(device.st_rdev))
assert root[root.index('-')+1] == 'ext4'
assert not Path('/var/lib/tdb-harness').exists()
assert not any(Path(p).exists() for p in ('/home/ubuntu/tdb-admin','/home/ubuntu/twitch-downloader-bot','/var/www/tdb'))
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
    peer.connect('/run/systemd/private')
    pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    assert pid == 1 and uid == 0
subprocess.run(['/usr/sbin/e2label','/dev/vda1','TDB-HARNESS'],check=True,timeout=15)
for filename in ('/etc/fstab','/boot/grub/grub.cfg'):
    p = Path(filename)
    content = p.read_text()
    p.write_text(content.replace('LABEL=cloudimg-rootfs','LABEL=TDB-HARNESS'))
subprocess.run(['/usr/bin/udevadm','trigger','--action=change','/sys/class/block/vda1'],check=True,timeout=15)
subprocess.run(['/usr/bin/udevadm','settle','--timeout=15'],check=True,timeout=20)
Path('/mnt/tdb-auto').mkdir()
subprocess.run(['/usr/bin/mount','-o','ro','/dev/disk/by-label/TDB_UPDATE','/mnt/tdb-auto'],check=True,timeout=15)
payload = Path('/mnt/tdb-auto')
data = (payload/'bundle.json').read_bytes()
assert hashlib.sha256(data).hexdigest() == config['manifest_sha256']
manifest = json.loads(data)
assert manifest['base'] == config['base'] and manifest['harness'] == config['harness']
for name, sha in manifest['sha256'].items():
    assert '..' not in Path(name).parts and not Path(name).is_absolute()
    assert hashlib.sha256((payload/name).read_bytes()).hexdigest() == sha
sys.path.insert(0,str(payload))
import auto_guest
auto_guest.install(config)
'''


def checksum(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def executable(name):
    path = shutil.which(name)
    if not path and sys.platform == 'win32':
        candidate = Path('C:/Program Files/qemu') / (name + '.exe')
        if candidate.is_file():
            path = str(candidate)
    if not path:
        raise RuntimeError('missing prerequisite: ' + name)
    return path


def windows_memory():
    if sys.platform != 'win32':
        return
    import ctypes
    class Memory(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page_total', 'page_available',
                                                    'virtual_total', 'virtual_available', 'extended')]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) or memory.available < 2*1024**3:
        raise RuntimeError('host requires at least 2 GiB available RAM')


def download(destination, deadline):
    temporary = destination.with_suffix('.part')
    count = 0
    with urllib.request.urlopen(IMAGE_URL, timeout=30) as source, temporary.open('xb') as output:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('Ubuntu image download deadline')
            chunk = source.read(512*1024)
            if not chunk:
                break
            count += len(chunk)
            if count > 2*1024**3:
                raise ValueError('Ubuntu image exceeds download limit')
            output.write(chunk)
    if checksum(temporary) != IMAGE_SHA256:
        raise ValueError('Ubuntu image checksum mismatch')
    os.replace(temporary, destination)


def seed_config(config):
    return dict(users=[], ssh_deletekeys=True, ssh_genkeytypes=[], ssh_pwauth=False,
                package_update=False, package_upgrade=False, network=dict(config='disabled'),
                growpart=dict(mode='auto', devices=['/']), resize_rootfs=True,
                write_files=[dict(path='/run/tdb-auto-config.json', permissions='0600',
                                  content=json.dumps(config)),
                             dict(path='/usr/local/sbin/tdb-auto-bootstrap.py', permissions='0700', content=BOOTSTRAP)],
                runcmd=[['/usr/bin/python3', '-I', '-S', '-B', '/usr/local/sbin/tdb-auto-bootstrap.py']])


def qemu_args(program, root, image, seed, port, identity, acceleration, boot):
    for path in (root, image, seed):
        host.safe_path(path)
    return [program, '-nodefaults', '-machine', 'q35', '-accel', acceleration,
            '-m', '954', '-smp', '1', '-display', 'none', '-monitor', 'none', '-nic', 'none',
            '-no-reboot', '-rtc', 'base=2030-01-01T12:00:00,clock=vm',
            '-smbios', f'type=1,manufacturer=TDB-Harness,product=TDB-Isolated-VM,uuid={identity}',
            '-serial', 'file:' + str(root / f'serial-boot{boot}.log'),
            '-drive', f'file={root / "guest.qcow2"},format=qcow2,if=virtio',
            '-drive', f'file={image},format=raw,media=cdrom,readonly=on',
            '-drive', f'file={seed},format=raw,media=cdrom,readonly=on',
            '-device', 'virtio-serial-pci',
            '-chardev', f'socket,id=tdb,host=127.0.0.1,port={port},server=on,wait=off',
            '-device', 'virtserialport,chardev=tdb,name=org.tdb.harness']


def validate_suite(report, config):
    if (report.get('base') != config['base'] or report.get('harness') != config['harness']
            or report.get('cleanup') != 'PASS' or report.get('kind') != 'REAL_SYSTEMD_WITH_SYNTHETIC_TOOLS'
            or [item.get('name') for item in report.get('cases', [])] != list(wire.EXPECTED)
            or any(item.get('status') != 'PASS' for item in report['cases'])):
        raise ValueError('suite report did not prove all expected cases/cleanup/version')


class Session:
    def __init__(self, root, config, deadline):
        self.root, self.config, self.deadline = root, config, deadline
        self.receiver = wire.Receiver(root / 'artifacts')
        self.process = self.connection = None
        self.buffer = bytearray()
        self.summary = dict(status='FAIL', base=config['base'], harness=config['harness'],
                            run_id=config['run_id'], image_sha256=IMAGE_SHA256, boots=[], powercut=False,
                            cases=[dict(name=name, status='NOT RUN') for name in wire.EXPECTED])

    def message(self):
        # Buffered frames must not bypass the overall deadline either.
        if time.monotonic() >= self.deadline:
            raise TimeoutError('VM run exceeded overall deadline')
        while b'\n' not in self.buffer:
            if time.monotonic() >= self.deadline:
                raise TimeoutError('VM run exceeded overall deadline')
            try:
                content = self.connection.recv(8192)
            except socket.timeout:
                if self.process.poll() is not None:
                    raise RuntimeError('QEMU exited before completing the protocol')
                continue
            if not content:
                raise RuntimeError('QEMU control channel closed')
            self.buffer.extend(content)
            if b'\n' not in self.buffer and len(self.buffer) > wire.MAX_LINE:
                raise ValueError('control line exceeds limit')
        line, _, remaining = self.buffer.partition(b'\n')
        self.buffer = bytearray(remaining)
        result = wire.decode(line + b'\n')
        if result.get('run_id') != self.config['run_id']:
            raise ValueError('wrong guest session')
        return result

    def stop(self, powercut=False):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()  # ONLY the QEMU process created by this session.
            self.process.wait(timeout=15)
            if powercut:
                self.summary['powercut'] = dict(method='kill-owned-QEMU-process', returncode=self.process.returncode,
                                                disk='guest.qcow2')
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def boot(self, program, image, seed, acceleration, index):
        # Reserve a loopback byte-channel port. It is never a guest NIC/network.
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        args = qemu_args(program, self.root, image, seed, port, self.config['run_id'], acceleration, index)
        log = (self.root / f'qemu-boot{index}.log').open('xb')
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                            env={key: value for key, value in os.environ.items()
                                                 if key.upper() in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'LANG')})
        finally:
            log.close()
        connect_deadline = min(self.deadline, time.monotonic()+30)
        while True:
            if self.process.poll() is not None:
                raise RuntimeError('QEMU startup failed; see qemu log')
            try:
                self.connection = socket.create_connection(('127.0.0.1', port), timeout=1)
                self.connection.settimeout(1)
                break
            except OSError:
                if time.monotonic() >= connect_deadline:
                    raise TimeoutError('QEMU control port startup timeout')
                time.sleep(.1)
        self.buffer.clear()
        original_deadline = self.deadline
        # The pinned offline cloud image waits ~120s for network-online before
        # cloud-init final/provisioning. Real hosted TCG reached final at 288s,
        # before any controller ran. Budget that boot separately from readiness.
        boot_seconds = 480 if index == 1 else 300
        self.deadline = min(original_deadline, time.monotonic()+boot_seconds)
        print(f'BOOT {index}: waiting for guest handshake (up to {boot_seconds}s, within overall budget)', flush=True)
        try:
            hello = self.message()
        except TimeoutError:
            raise TimeoutError(f'guest boot {index} handshake deadline (up to {boot_seconds}s within overall budget); '
                               f'see serial-boot{index}.log') from None
        finally:
            self.deadline = original_deadline
        expected_phase = 'suite' if index == 1 else 'resume'
        if (hello.get('type') != 'hello' or hello.get('phase') != expected_phase
                or hello.get('base') != self.config['base'] or hello.get('harness') != self.config['harness']
                or not hello.get('systemd', '').startswith('systemd 255') or not hello.get('python', '').startswith('3.12.')):
            raise ValueError('guest version/boot handshake mismatch')
        if index == 2 and hello['boot_id'] == self.summary['boots'][0]['boot_id']:
            raise ValueError('VM did not undergo a real new kernel boot')
        if str(uuid.UUID(hello['boot_id'])) != hello['boot_id']:
            raise ValueError('invalid kernel boot ID')
        self.summary['boots'].append(hello)
        self.connection.sendall(wire.encode(dict(type='host_ready', run_id=self.config['run_id'])))
        print(f'BOOT {index}: {expected_phase}, {hello["systemd"]}, Python {hello["python"]}', flush=True)

    def consume(self, index):
        suite_passed = False
        while True:
            message = self.message()
            kind = message['type']
            if self.receiver.current is not None and not kind.startswith('file_'):
                raise ValueError('control event inside an unfinished artifact')
            if kind.startswith('file_'):
                self.receiver.accept(message)
            elif kind == 'case':
                if message.get('name') not in wire.EXPECTED or message.get('status') not in ('RUN', 'PASS'):
                    raise ValueError('invalid live scenario event')
                for case in self.summary['cases']:
                    if case['name'] == message['name']:
                        case['status'] = message['status']
                print(message['status'] + ' ' + message['name'], flush=True)
            elif kind == 'suite_pass' and index == 1:
                name = wire.valid_name('suite/reports/' + message['report'])
                if name not in self.receiver.files:
                    raise ValueError('suite report was not transferred/verified')
                report = json.loads((self.root / 'artifacts' / name).read_text())
                validate_suite(report, self.config)
                self.summary['suite_report'] = name
                self.summary['cases'] = [dict(name=item['name'], status=item['status']) for item in report['cases']]
                suite_passed = True
                print('SUITE: all 01-13 and 15-16 passed; cleanup verified', flush=True)
            elif kind == 'powercut_ready' and index == 1 and suite_passed:
                name = 'powercut/metadata/powercut.json'
                if name not in self.receiver.files or message['boot_id'] != self.summary['boots'][0]['boot_id']:
                    raise ValueError('missing powercut checkpoint')
                checkpoint = json.loads((self.root / 'artifacts' / name).read_text())
                if checkpoint['owner']['boot_id'] != message['boot_id']:
                    raise ValueError('wrong crash owner')
                self.stop(powercut=True)
                if not self.summary['powercut']:
                    raise ValueError('QEMU had already exited; no controlled power loss')
                print('POWER LOSS: whole QEMU killed; preserving the same disk', flush=True)
                return
            elif kind == 'complete':
                if not message.get('success') or index != 2:
                    raise RuntimeError('guest failed: ' + json.dumps(message))
                name = 'resume/metadata/reboot-result.json'
                if name not in self.receiver.files:
                    raise ValueError('missing independently transferred reboot result')
                result = json.loads((self.root / 'artifacts' / name).read_text())
                if (result.get('status') != 'PASS' or result.get('old_boot') != self.summary['boots'][0]['boot_id']
                        or result.get('new_boot') != self.summary['boots'][1]['boot_id']
                        or result.get('base') != self.config['base'] or result.get('harness') != self.config['harness']):
                    raise ValueError('reboot recovery report/version mismatch')
                self.summary['reboot_report'] = name
                self.process.wait(timeout=60)
                if self.process.returncode != 0:
                    raise RuntimeError('final QEMU shutdown was not clean')
                self.summary['scenario14'] = 'PASS'
                self.summary['status'] = 'PASS'
                return
            elif kind != 'diagnostic':
                raise ValueError('unexpected control message/order: ' + str(kind))

    def finish(self):
        self.stop()
        self.receiver.close()
        for case in self.summary['cases']:
            if case['status'] == 'RUN':
                case['status'] = 'FAIL/INTERRUPTED'
        self.summary['qemu_stopped'] = self.process is None or self.process.poll() is not None
        self.summary['artifacts'] = self.receiver.files
        (self.root / 'summary.json').write_text(json.dumps(self.summary, indent=2))
        text = '# TDB systemd integration: ' + self.summary['status'] + '\n\n'
        text += f'Source: `{self.config["base"]}`\n\nHarness: `{self.config["harness"]}`\n\n'
        text += '| Scenario | Result |\n|---|---|\n'
        for case in self.summary.get('cases', []):
            text += '| ' + case['name'] + ' | ' + case['status'] + ' |\n'
        text += '| 14 VM power loss / new kernel boot | ' + self.summary.get('scenario14', 'NOT PASSED') + ' |\n\n'
        text += 'QEMU stopped: ' + str(self.summary['qemu_stopped']) + '\n\n'
        text += 'See summary.json, serial/QEMU logs and checksum-verified artifacts.\n'
        if 'error' in self.summary:
            text += '\nFailure: ' + self.summary['error'] + '\n'
        (self.root / 'report.md').write_text(text)


def run(output, image=None, acceleration='auto', timeout=1800):
    if timeout < 60 or timeout > 2400:
        raise ValueError('run timeout must be 60..2400 seconds')
    output = host.safe_path(output)
    output.mkdir(mode=0o700)  # NEW directory only; never touch an existing user VM.
    deadline = time.monotonic()+timeout
    config = dict(run_id=str(uuid.uuid4()), base=release.BASE)
    session = None
    try:
        host.check_host()
        windows_memory()
        program, img_tool = executable('qemu-system-x86_64'), executable('qemu-img')
        if shutil.disk_usage(output).free < 8*1024**3:
            raise RuntimeError('host requires at least 8 GiB free disk')
        if image is None:
            image = output / 'ubuntu-base.img'
            download(image, min(deadline, time.monotonic()+480))
        image = host.safe_path(image)
        if checksum(image) != IMAGE_SHA256:
            raise ValueError('wrong Ubuntu base image checksum')
        payload = output / 'payload'
        host.bundle(payload)
        manifest = json.loads((payload / 'bundle.json').read_text())
        config.update(harness=manifest['harness'], manifest_sha256=checksum(payload / 'bundle.json'))
        legacy = {}
        repo = Path(__file__).resolve().parents[2]
        for name, allowed in release.OLD_HASHES.items():
            revision = release.OLD_BASE if name in release.SOURCE else release.OLD_HARNESS
            path = name if name in release.SOURCE else 'tests/systemd_harness/' + name
            data = host.git_bytes(repo, 'show', revision + ':' + path)
            if hashlib.sha256(data).hexdigest() not in allowed:
                raise ValueError('legacy fixture differs from reviewed commit')
            legacy[name] = base64.b64encode(data).decode('ascii')
        (payload / 'legacy.json').write_text(json.dumps(legacy))
        config['legacy_sha256'] = checksum(payload / 'legacy.json')
        media = output / 'payload.iso'
        iso.create(payload, media, [*manifest['sha256'], 'bundle.json', 'legacy.json'])
        seed_dir = output / 'seed'
        seed_dir.mkdir(mode=0o700)
        (seed_dir / 'user-data').write_text('#cloud-config\n' + json.dumps(seed_config(config)))
        (seed_dir / 'meta-data').write_text(json.dumps({'instance-id': config['run_id'], 'local-hostname': 'tdb-test'}))
        seed = output / 'seed.iso'
        iso.create(seed_dir, seed, ['user-data', 'meta-data'], volume='CIDATA')
        subprocess.run([img_tool, 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(image),
                        str(output / 'guest.qcow2'), '32G'], check=True, capture_output=True, timeout=30)
        if acceleration == 'auto':
            acceleration = 'kvm' if sys.platform == 'linux' and os.access('/dev/kvm', os.R_OK | os.W_OK) else 'tcg'
        session = Session(output, config, deadline)
        session.summary.update(acceleration=acceleration, payload_iso_sha256=checksum(media), seed_iso_sha256=checksum(seed),
                               guest_clock='2030-01-01T12:00:00; no NTP/network')
        for index in (1, 2):
            session.boot(program, media, seed, acceleration, index)
            session.consume(index)
    except BaseException as error:
        if session:
            session.summary['error'] = type(error).__name__ + ': ' + str(error)[:1800]
        else:
            (output / 'summary.json').write_text(json.dumps(dict(status='BLOCKED', error=str(error)[:1800], **config), indent=2))
            (output / 'report.md').write_text('# TDB systemd integration: BLOCKED\n\n' + str(error)[:1800] + '\n')
        raise
    finally:
        if session:
            session.finish()


def main():
    def interrupted(*_):
        raise KeyboardInterrupt('runner interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--image', type=Path, help='optional cached image; must match the fixed SHA-256')
    parser.add_argument('--accel', choices=('auto', 'kvm', 'tcg'), default='auto')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    output = args.output or Path.cwd() / '_work' / ('systemd-run-' + uuid.uuid4().hex[:12])
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        run(output, args.image, args.accel, args.timeout)
    except BaseException as error:
        print('FAIL/BLOCKED: ' + str(error), file=sys.stderr)
        print('DIAGNOSTICS: ' + str(output), file=sys.stderr)
        raise SystemExit(1)
    print('PASS: ' + str(output / 'report.md'))


if __name__ == '__main__':
    main()
