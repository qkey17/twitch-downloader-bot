"""Optional local maintenance handshake; no Telegram commands or database writes."""
import asyncio
import errno
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import struct
import uuid


GATE_KEY = 'tdb_maintenance_gate'
SERVICE_KEY = 'tdb_maintenance_service'
MAX_MESSAGE = 4096
log = logging.getLogger(__name__)


class MaintenanceError(RuntimeError):
    pass


def admission_closed(application):
    return application.bot_data.get(GATE_KEY) is not None


class Coordinator:
    """All admission transitions run on the same event loop as begin_download."""
    def __init__(self, application, pause, resume, persist=None, clear=None):
        self.app, self.pause, self.resume = application, pause, resume
        self.persist, self.clear = persist, clear
        self.token = self.owner = None
        self.committed = self.shutting_down = False

    def response(self, status):
        return dict(version=1, status=status, pid=os.getpid(), token=self.token,
                    committed=self.committed, running=bool(self.app.running and not self.shutting_down))

    async def command(self, action, token, owner):
        if action == 'status':
            return self.response('running' if self.app.running and not self.shutting_down else 'unavailable')
        if action == 'prepare':
            if not self.app.running or self.shutting_down:
                return self.response('unavailable')
            if self.token is not None or self.app.bot_data.get('vod_global_operation') is not None:
                return self.response('busy')
            # No await may occur between the busy check and closing admission.
            self.token, self.owner = token, owner
            self.app.bot_data[GATE_KEY] = token
            try:
                await self.pause(self.app)
            except BaseException:
                if self.token == token and self.owner is owner:
                    await self._release()
                raise
            if self.token != token or self.owner is not owner or self.app.bot_data.get(GATE_KEY) != token:
                return self.response('unavailable')
            if not self.app.running or self.shutting_down:
                await self._release()
                return self.response('unavailable')
            return self.response('ready')
        if action == 'commit':
            if self.token != token or self.owner is not owner or self.shutting_down:
                return self.response('unavailable')
            # An EOF after this fence cannot admit a VOD ahead of a queued stop.
            self.committed = True
            if self.persist is not None:
                self.persist(token)  # Durable and fsynced before acknowledging COMMIT.
            return self.response('committed')
        if action == 'release':
            if self.token != token or self.shutting_down:
                return self.response('unavailable')
            await self._release()
            return self.response('released')
        return self.response('unavailable')

    async def _release(self):
        if self.committed and self.clear is not None:
            self.clear(self.token)  # Failure keeps both the gate and ownership proof.
        if self.shutting_down:
            self.app.bot_data[GATE_KEY] = 'shutdown'
        else:
            self.app.bot_data.pop(GATE_KEY, None)
        self.token = self.owner = None
        self.committed = False
        if self.app.running and not self.shutting_down:
            await self.resume(self.app)

    def inherit(self, token):
        self.token, self.owner, self.committed = token, None, True
        self.app.bot_data[GATE_KEY] = token

    async def disconnect(self, owner):
        if self.owner is owner:
            if self.committed:
                self.owner = None
            else:
                await self._release()

    def begin_shutdown(self):
        self.shutting_down = True
        self.app.bot_data[GATE_KEY] = self.token or 'shutdown'


def _decode(line):
    if not line.endswith(b'\n') or len(line) > MAX_MESSAGE:
        raise MaintenanceError('Invalid maintenance message length')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MaintenanceError('Duplicate maintenance field')
            result[key] = value
        return result
    try:
        value = json.loads(line, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise MaintenanceError('Invalid maintenance JSON') from None
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise MaintenanceError('Unsupported maintenance protocol')
    action = value.get('action')
    if action == 'status' and set(value) == {'version', 'action'}:
        return action, None
    if (not isinstance(action, str) or action not in {'prepare', 'commit', 'release'}
            or set(value) != {'version', 'action', 'token'}
            or not isinstance(value['token'], str) or not re.fullmatch('[0-9a-f]{32}', value['token'])):
        raise MaintenanceError('Invalid maintenance command')
    return action, value['token']


def _identity(info):
    return info.st_dev, info.st_ino


def _parent(path):
    if not path.is_absolute() or '..' in path.parts or len(os.fsencode(path)) > 100:
        raise MaintenanceError('Use a short absolute maintenance socket path without traversal')
    for part in (*reversed(path.parent.parents), path.parent):
        if part.is_symlink():
            raise MaintenanceError('Maintenance socket parents cannot be symlinks')
    info = path.parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise MaintenanceError('Maintenance socket requires an existing owner-only directory')


def _marker_stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_committed(path):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or path.is_symlink()
            or (os.name == 'posix' and (before.st_uid != os.geteuid() or before.st_mode & 0o077))):
        raise MaintenanceError('Unsafe durable maintenance fence')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        content = os.read(fd, 257)
        if (len(content) > 256 or os.read(fd, 1) or _marker_stamp(os.fstat(fd)) != _marker_stamp(before)
                or _marker_stamp(path.lstat()) != _marker_stamp(before)):
            raise MaintenanceError('Durable maintenance fence changed or exceeded its bound')
    finally:
        os.close(fd)
    # Reuse strict duplicate-field/version/token parsing; stored data has no action.
    try:
        value = json.loads(content, object_pairs_hook=_marker_object)
    except (ValueError, UnicodeError, RecursionError):
        raise MaintenanceError('Invalid durable maintenance fence') from None
    if (not isinstance(value, dict) or set(value) != {'version', 'token'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['token'], str) or not re.fullmatch('[0-9a-f]{32}', value['token'])):
        raise MaintenanceError('Unrecognized durable maintenance fence')
    return value['token'], _marker_stamp(before)


def _marker_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MaintenanceError('Duplicate durable maintenance field')
        result[key] = value
    return result


def _sync_parent(path):
    if os.name == 'posix':
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _persist_committed(path, token):
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise MaintenanceError('Invalid durable maintenance token')
    existing = _read_committed(path)
    if existing is not None:
        if existing[0] != token:
            raise MaintenanceError('A different durable maintenance fence exists')
        _sync_parent(path)
        return
    temporary = path.parent/('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        content = json.dumps(dict(version=1, token=token), separators=(',', ':')).encode('ascii')
        if os.write(fd, content) != len(content):
            raise MaintenanceError('Incomplete durable maintenance fence write')
        os.fsync(fd)
    finally:
        os.close(fd)
    # A private parent and the persistent server flock serialize all marker writers.
    # Never replace an existing marker, including a symlink or a different token.
    if path.exists() or path.is_symlink():
        raise MaintenanceError('Durable maintenance fence appeared during commit')
    os.replace(temporary, path)
    _sync_parent(path)


def _clear_committed(path, token):
    existing = _read_committed(path)
    if existing is None:
        # COMMIT may have failed before publication, or a previous RELEASE may
        # have unlinked successfully before fsync failed. The coordinator still
        # checks the live token; recovery must also prove no stop job is pending.
        _sync_parent(path)
        return
    if existing[0] != token:
        raise MaintenanceError('Durable maintenance fence does not match release ownership')
    if os.name == 'posix':
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _marker_stamp(os.stat(path.name, dir_fd=fd, follow_symlinks=False)) != existing[1]:
                raise MaintenanceError('Durable maintenance fence was replaced')
            os.unlink(path.name, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        if _marker_stamp(path.lstat()) != existing[1]:
            raise MaintenanceError('Durable maintenance fence was replaced')
        path.unlink()


class LocalServer:
    def __init__(self, application, path, pause, resume):
        self.path = path
        self.marker = path.parent/(path.name+'.committed')
        self.coordinator = Coordinator(application, pause, resume,
            persist=lambda token: _persist_committed(self.marker, token),
            clear=lambda token: _clear_committed(self.marker, token))
        self.server = self.lock = self.identity = None
        self.started = False
        self.clients, self.tasks = set(), set()

    async def start(self):
        if os.name != 'posix' or not hasattr(socket, 'SO_PEERCRED'):
            raise MaintenanceError('Maintenance IPC requires Linux peer credentials')
        import fcntl
        _parent(self.path)
        lock_path = self.path.parent/(self.path.name+'.lock')
        self.lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or _identity(info) != _identity(lock_path.lstat())):
                raise MaintenanceError('Unsafe maintenance socket lock')
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise MaintenanceError('Another bot owns the maintenance socket') from None
            existing = _read_committed(self.marker)
            if existing is not None:
                self.coordinator.inherit(existing[0])
                await self.coordinator.pause(self.coordinator.app)
            self._remove_stale()
            self.server = await asyncio.start_unix_server(self._client, path=str(self.path), limit=MAX_MESSAGE)
            self.identity = _identity(self.path.lstat())
            os.chmod(self.path, 0o600)
            self.started = True
        except BaseException:
            await self.close()
            raise

    def _remove_stale(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise MaintenanceError('Existing maintenance path is not an owned socket')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise MaintenanceError('Cannot establish that maintenance socket is stale') from None
            else:
                raise MaintenanceError('Existing maintenance listener must not be replaced')
        if _identity(self.path.lstat()) != _identity(info):
            raise MaintenanceError('Maintenance socket changed while checking')
        self.path.unlink()  # Specific stale socket only; protected by persistent flock.

    async def _client(self, reader, writer):
        owner = object()
        task = asyncio.current_task()
        self.tasks.add(task)
        self.clients.add(writer)
        try:
            connection = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid() or len(self.clients) > 4:
                return
            while not self.coordinator.shutting_down:
                line = await reader.readline()
                if not line:
                    break
                action, token = _decode(line)
                result = await self.coordinator.command(action, token, owner)
                writer.write(json.dumps(result, separators=(',', ':')).encode('ascii')+b'\n')
                await writer.drain()
        except (OSError, ValueError, MaintenanceError, RuntimeError) as error:
            log.warning('Maintenance request refused: %s', type(error).__name__)
        finally:
            try:
                await self.coordinator.disconnect(owner)
            except Exception as error:
                log.error('Maintenance release failed: %s', type(error).__name__)
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass
            self.tasks.discard(task)

    async def close(self):
        if self.started or self.server is not None:
            self.coordinator.begin_shutdown()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in tuple(self.clients):
            writer.close()
        # All tasks are explicitly owned, separate from PTB's shutdown task set.
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.identity is not None:
            try:
                if _identity(self.path.lstat()) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self.identity = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


async def start_maintenance(app):
    value = os.environ.get('TCD_MAINTENANCE_SOCKET')
    if not value:
        return
    from .html_cleanup import start_cleanup, stop_cleanup
    service = LocalServer(app, Path(value), stop_cleanup, start_cleanup)
    app.bot_data[SERVICE_KEY] = service
    await service.start()


async def stop_maintenance(app):
    service = app.bot_data.get(SERVICE_KEY)
    if service is not None:
        await service.close()
