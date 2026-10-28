"""Bounded data-only protocol on a dedicated QEMU virtio-serial byte channel."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

MAX_LINE = 96 * 1024
MAX_FILE = 4 * 1024**2
MAX_TOTAL = 64 * 1024**2
CHUNK = 48 * 1024
EXPECTED = ('01-success-repeat', '02-busy-vod', '03-sigterm', '04-sigkill-owner',
            '05-sigkill-stop-job', '06a-snapshot-error', '06b-verify-error', '07-start-error',
            '08-slow-ready', '09-auto-restart', '10-attempt-limit', '11-read-errors-INJECTED',
            '12-parallel-boot', '13-persistent-catchup', '15-hold-locks-concurrency', '16-offsite-fence')


def encode(message):
    result = json.dumps(message, separators=(',', ':')).encode() + b'\n'
    if len(result) > MAX_LINE:
        raise ValueError('oversize control message')
    return result


def decode(line):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate control field')
            result[key] = value
        return result
    if len(line) > MAX_LINE or not line.endswith(b'\n'):
        raise ValueError('invalid control framing')
    result = json.loads(line, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError('invalid control object')
    return result


def valid_name(name):
    if not isinstance(name, str) or '\\' in name or '..' in PurePosixPath(name).parts:
        raise ValueError('unsafe artifact path')
    pattern = (r'(?:suite|powercut|resume)/(?:reports|metadata|journals)/[a-z0-9][a-z0-9_.-]{0,110}'
               r'|(?:suite|powercut|resume)/cases/[0-9a-f]{32}/[a-z0-9][a-z0-9_.-]{0,110}')
    if not re.fullmatch(pattern, name):
        raise ValueError('artifact is outside the allowlist')
    return name


class Receiver:
    def __init__(self, root):
        self.root, self.current, self.total, self.files = root, None, 0, {}

    def close(self):
        if self.current:
            self.current['file'].close()
            self.current = None

    def accept(self, message):
        kind = message['type']
        if kind == 'file_begin':
            if self.current or len(self.files) >= 512:
                raise ValueError('nested/too many artifacts')
            name = valid_name(message['name'])
            size, sha = message['size'], message['sha256']
            if (type(size) is not int or not 0 <= size <= MAX_FILE or self.total+size > MAX_TOTAL
                    or not re.fullmatch('[0-9a-f]{64}', sha) or name in self.files):
                raise ValueError('invalid artifact manifest')
            path = self.root / name
            for parent in (*reversed(path.parents), path):
                if parent.is_symlink():
                    raise ValueError('artifact symlink')
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.current = dict(file=path.open('xb'), name=name, size=size, sha=sha,
                                seen=0, digest=hashlib.sha256())
        elif kind == 'file_chunk':
            if self.current is None:
                raise ValueError('chunk without manifest')
            content = base64.b64decode(message['data'], validate=True)
            self.current['seen'] += len(content)
            if len(content) > CHUNK or self.current['seen'] > self.current['size']:
                raise ValueError('artifact exceeds declared size')
            self.current['file'].write(content)
            self.current['digest'].update(content)
        elif kind == 'file_end':
            current = self.current
            if (current is None or current['seen'] != current['size']
                    or current['digest'].hexdigest() != current['sha']):
                raise ValueError('artifact checksum mismatch')
            current['file'].close()
            self.files[current['name']] = dict(size=current['size'], sha256=current['sha'])
            self.total += current['size']
            self.current = None
        else:
            raise ValueError('unknown artifact message')
