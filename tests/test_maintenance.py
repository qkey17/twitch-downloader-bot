"""Local maintenance coordination; synthetic contexts, never a running bot."""
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src import maintenance
from src.handlers.handlers_state import begin_download, finish_download, is_globally_busy


TOKEN = 'a'*32
OTHER = 'b'*32


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    def setUp(self):
        self.app = SimpleNamespace(running=True, bot_data={})
        self.context = SimpleNamespace(application=self.app, user_data={})
        self.pause, self.resume = AsyncMock(), AsyncMock()
        self.gate = maintenance.Coordinator(self.app, self.pause, self.resume)
        self.owner = object()

    async def command(self, action, token=TOKEN, owner=None):
        return await self.gate.command(action, token, self.owner if owner is None else owner)

    async def test_busy_vod_is_not_cancelled_and_prepare_does_not_hold_admission(self):
        operation = begin_download(self.context)
        result = await self.command('prepare')
        self.assertEqual('busy', result['status'])
        self.assertFalse(maintenance.admission_closed(self.app))
        self.assertEqual(operation, self.app.bot_data['vod_global_operation'])
        self.assertNotIn('vod_cancel', self.context.user_data)
        self.pause.assert_not_awaited()
        finish_download(self.context, operation)
        self.assertEqual('ready', (await self.command('prepare'))['status'])

    async def test_gate_closes_synchronously_before_cleanup_is_drained(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def pause(_app):
            entered.set()
            await finish.wait()
        self.gate.pause = pause
        preparation = asyncio.create_task(self.command('prepare'))
        await entered.wait()
        self.assertTrue(maintenance.admission_closed(self.app))
        self.assertTrue(is_globally_busy(self.context))
        self.assertIsNone(begin_download(self.context))
        self.assertFalse(preparation.done())
        finish.set()
        result = await preparation
        self.assertEqual('ready', result['status'])
        self.assertEqual(TOKEN, result['token'])
        self.assertFalse(result['committed'])

    async def test_prepare_disconnect_releases_and_resumes_cleanup(self):
        await self.command('prepare')
        await self.gate.disconnect(self.owner)
        self.assertFalse(maintenance.admission_closed(self.app))
        self.resume.assert_awaited_once_with(self.app)
        self.assertIsNotNone(begin_download(self.context))

    async def test_committed_disconnect_stays_fenced_until_authenticated_recovery(self):
        await self.command('prepare')
        self.assertEqual('committed', (await self.command('commit'))['status'])
        await self.gate.disconnect(self.owner)
        self.assertTrue(maintenance.admission_closed(self.app))
        self.assertIsNone(begin_download(self.context))
        self.resume.assert_not_awaited()
        self.assertEqual('busy', (await self.command('prepare', OTHER, object()))['status'])
        self.assertEqual('unavailable', (await self.command('release', OTHER, object()))['status'])
        self.assertEqual('released', (await self.command('release', TOKEN, object()))['status'])
        self.assertFalse(maintenance.admission_closed(self.app))
        self.resume.assert_awaited_once()

    async def test_commit_requires_the_owning_connection_and_token(self):
        await self.command('prepare')
        self.assertEqual('unavailable', (await self.command('commit', OTHER))['status'])
        self.assertEqual('unavailable', (await self.command('commit', TOKEN, object()))['status'])
        self.assertFalse(self.gate.committed)
        await self.gate.disconnect(self.owner)
        self.assertFalse(maintenance.admission_closed(self.app))

    async def test_cleanup_failure_cannot_acknowledge_safe_stop(self):
        self.pause.side_effect = RuntimeError('synthetic cleanup failure')
        with self.assertRaises(RuntimeError):
            await self.command('prepare')
        self.assertFalse(maintenance.admission_closed(self.app))
        self.assertIsNone(self.gate.token)
        self.resume.assert_awaited_once()

    async def test_release_during_drain_cannot_produce_a_stale_ready_ack(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def pause(_app):
            entered.set()
            await finish.wait()
        self.gate.pause = pause
        preparation = asyncio.create_task(self.command('prepare'))
        await entered.wait()
        await self.command('release', TOKEN, object())
        finish.set()
        self.assertEqual('unavailable', (await preparation)['status'])
        self.assertFalse(maintenance.admission_closed(self.app))

    async def test_stopped_or_shutting_down_app_cannot_authorize_maintenance(self):
        self.app.running = False
        self.assertEqual('unavailable', (await self.command('prepare'))['status'])
        self.app.running = True
        await self.command('prepare')
        self.gate.begin_shutdown()
        await self.gate.disconnect(self.owner)
        self.resume.assert_not_awaited()
        self.assertTrue(maintenance.admission_closed(self.app))
        self.assertEqual('unavailable', (await self.command('status'))['status'])
        self.assertEqual('unavailable', (await self.command('prepare'))['status'])

    async def test_default_start_is_disabled_without_opt_in_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(maintenance, 'LocalServer') as server:
            await maintenance.start_maintenance(self.app)
        server.assert_not_called()
        self.assertFalse(maintenance.admission_closed(self.app))

    async def test_commit_persists_before_ack_and_inherited_fence_blocks_new_vod(self):
        stored = []
        self.gate.persist = lambda token: stored.append(token)
        await self.command('prepare')
        result = await self.command('commit')
        self.assertEqual([TOKEN], stored)
        self.assertEqual('committed', result['status'])
        restarted = maintenance.Coordinator(self.app, self.pause, self.resume)
        restarted.inherit(stored[0])
        self.assertTrue(restarted.committed)
        self.assertIsNone(begin_download(self.context))
        self.assertEqual('busy', (await restarted.command('prepare', OTHER, object()))['status'])
        self.assertEqual('released', (await restarted.command('release', TOKEN, object()))['status'])

    async def test_persist_and_clear_failures_keep_admission_closed(self):
        def fail(_token):
            raise OSError('synthetic durable-write failure')
        await self.command('prepare')
        self.gate.persist = fail
        with self.assertRaises(OSError): await self.command('commit')
        self.assertTrue(self.gate.committed)
        await self.gate.disconnect(self.owner)
        self.assertIsNone(begin_download(self.context))
        self.gate.clear = fail
        with self.assertRaises(OSError): await self.command('release', owner=object())
        self.assertEqual(TOKEN, self.gate.token)
        self.assertTrue(self.gate.committed)
        self.resume.assert_not_awaited()


class ProtocolTests(unittest.TestCase):
    def test_only_bounded_exact_protocol_messages_are_accepted(self):
        self.assertEqual(('status', None), maintenance._decode(b'{"version":1,"action":"status"}\n'))
        self.assertEqual(('prepare', TOKEN), maintenance._decode(
            json.dumps(dict(version=1, action='prepare', token=TOKEN)).encode()+b'\n'))
        invalid = [b'{}\n', b'not-json\n', b'{"version":true,"action":"status"}\n',
                   b'{"version":1,"version":1,"action":"status"}\n',
                   b'{"version":1,"action":"status","extra":0}\n',
                   b'{"version":1,"action":"status"}', b'x'*4097+b'\n']
        for token in (None, '../escape', 'AGE-SECRET-KEY-1', 'a'*31):
            invalid.append(json.dumps(dict(version=1, action='prepare', token=token)).encode()+b'\n')
        for line in invalid:
            with self.subTest(line=line[:60]):
                with self.assertRaises(maintenance.MaintenanceError): maintenance._decode(line)


class DurableFenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='tdb-maint-marker-', dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'control.sock.committed'

    def test_marker_is_complete_private_and_only_matching_release_removes_it(self):
        maintenance._persist_committed(self.path, TOKEN)
        saved = self.path.read_bytes()
        self.assertEqual(TOKEN, maintenance._read_committed(self.path)[0])
        with self.assertRaises(maintenance.MaintenanceError): maintenance._persist_committed(self.path, OTHER)
        with self.assertRaises(maintenance.MaintenanceError): maintenance._clear_committed(self.path, OTHER)
        self.assertEqual(saved, self.path.read_bytes())
        maintenance._persist_committed(self.path, TOKEN)  # Idempotent same-owner commit.
        self.assertEqual(saved, self.path.read_bytes())
        if os.name == 'posix': self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        maintenance._clear_committed(self.path, TOKEN)
        self.assertFalse(self.path.exists())

    def test_invalid_or_hardlinked_marker_never_opens_gate_or_is_overwritten(self):
        for data in (b'not json', b'{"version":1,"token":"' + TOKEN.encode()+b'","token":"'+TOKEN.encode()+b'"}',
                     b'x'*257):
            with self.subTest(data=data[:25]):
                self.path.write_bytes(data)
                with self.assertRaises(maintenance.MaintenanceError): maintenance._read_committed(self.path)
                with self.assertRaises(maintenance.MaintenanceError): maintenance._persist_committed(self.path, TOKEN)
                self.assertEqual(data, self.path.read_bytes())
        self.path.unlink()
        maintenance._persist_committed(self.path, TOKEN)
        os.link(self.path, self.path.with_name('another-link'))
        with self.assertRaises(maintenance.MaintenanceError): maintenance._read_committed(self.path)
        with self.assertRaises(maintenance.MaintenanceError): maintenance._clear_committed(self.path, TOKEN)
        self.assertTrue(self.path.exists())

    def test_recovery_after_failed_commit_can_release_the_same_live_token(self):
        # No stop is permitted without COMMIT ACK. A failed pre-publication
        # write leaves no marker; authenticated recovery can clear its memory fence.
        maintenance._clear_committed(self.path,TOKEN)
        self.assertFalse(self.path.exists())
        maintenance._persist_committed(self.path,TOKEN)
        maintenance._clear_committed(self.path,TOKEN)
        maintenance._clear_committed(self.path,TOKEN)
        self.assertFalse(self.path.exists())


    def test_interruption_after_atomic_publication_leaves_a_readable_fence(self):
        with patch.object(maintenance,'_sync_parent',side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(OSError): maintenance._persist_committed(self.path,TOKEN)
        self.assertEqual(1,self.path.stat().st_nlink)
        self.assertEqual(TOKEN,maintenance._read_committed(self.path)[0])
        maintenance._persist_committed(self.path,TOKEN)
        maintenance._clear_committed(self.path,TOKEN)
        self.assertFalse(self.path.exists())


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'SO_PEERCRED'), 'Linux peer credentials required')
class LocalSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='tdb-maint-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root/'control.sock'
        self.app = SimpleNamespace(running=True, bot_data={})
        self.pause, self.resume = AsyncMock(), AsyncMock()
        self.service = maintenance.LocalServer(self.app, self.path, self.pause, self.resume)
        await self.service.start()
        self.addAsyncCleanup(self.service.close)

    async def connect(self):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        self.addAsyncCleanup(self.close_writer, writer)
        return reader, writer

    async def close_writer(self, writer):
        writer.close()
        await writer.wait_closed()

    async def request(self, connection, action, token=TOKEN):
        reader, writer = connection
        value = dict(version=1, action=action)
        if action != 'status': value['token'] = token
        writer.write(json.dumps(value).encode()+b'\n')
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 2))

    async def test_socket_commit_disconnect_and_recovery(self):
        first = await self.connect()
        result = await self.request(first, 'prepare')
        self.assertEqual('ready', result['status'])
        self.assertEqual(os.getpid(), result['pid'])
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        await self.request(first, 'commit')
        await self.close_writer(first[1])
        await asyncio.sleep(0)
        second = await self.connect()
        self.assertTrue((await self.request(second, 'status'))['committed'])
        self.assertEqual('released', (await self.request(second, 'release'))['status'])
        self.assertFalse(self.service.marker.exists())
        self.resume.assert_awaited_once()

    async def test_committed_marker_survives_server_close_and_is_inherited_after_restart(self):
        first = await self.connect()
        await self.request(first, 'prepare')
        await self.request(first, 'commit')
        await self.service.close()
        self.assertTrue(self.service.marker.exists())
        restarted_app = SimpleNamespace(running=True, bot_data={})
        restarted = maintenance.LocalServer(restarted_app, self.path, self.pause, self.resume)
        await restarted.start()
        self.addAsyncCleanup(restarted.close)
        self.assertTrue(maintenance.admission_closed(restarted_app))
        self.assertTrue(restarted.coordinator.committed)
        self.assertEqual(TOKEN, restarted.coordinator.token)
        self.pause.assert_awaited()
        self.assertEqual('released', (await restarted.coordinator.command('release', TOKEN, object()))['status'])
        self.assertFalse(restarted.marker.exists())
        self.assertFalse(maintenance.admission_closed(restarted_app))

    async def test_prepared_disconnect_and_malformed_message_release(self):
        first = await self.connect()
        await self.request(first, 'prepare')
        first[1].write(b'x'*5000+b'\n')
        await first[1].drain()
        self.assertEqual(b'', await asyncio.wait_for(first[0].read(), 2))
        self.assertFalse(maintenance.admission_closed(self.app))
        self.resume.assert_awaited_once()

    async def test_second_listener_and_foreign_path_are_never_replaced(self):
        second = maintenance.LocalServer(self.app, self.path, self.pause, self.resume)
        with self.assertRaises(maintenance.MaintenanceError): await second.start()
        self.assertTrue(self.path.exists())
        existing = self.root/'foreign'
        existing.write_text('keep', encoding='utf-8')
        foreign = maintenance.LocalServer(self.app, existing, self.pause, self.resume)
        with self.assertRaises(maintenance.MaintenanceError): await foreign.start()
        self.assertEqual('keep', existing.read_text(encoding='utf-8'))

    async def test_crash_stale_socket_is_recovered_only_under_exclusive_lock(self):
        await self.service.close()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(self.path))
        replacement = maintenance.LocalServer(self.app, self.path, self.pause, self.resume)
        await replacement.start()
        self.addAsyncCleanup(replacement.close)
        self.assertTrue(self.path.exists())

    async def test_symbolic_link_path_and_public_directory_are_refused(self):
        target = self.root/'target'
        target.write_text('keep', encoding='utf-8')
        link = self.root/'link.sock'
        link.symlink_to(target)
        linked = maintenance.LocalServer(self.app, link, self.pause, self.resume)
        with self.assertRaises(maintenance.MaintenanceError): await linked.start()
        self.assertEqual('keep', target.read_text(encoding='utf-8'))
        public = self.root/'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        unsafe = maintenance.LocalServer(self.app, public/'control.sock', self.pause, self.resume)
        with self.assertRaises(maintenance.MaintenanceError): await unsafe.start()

    async def test_shutdown_does_not_restart_cleanup_and_unlinks_only_owned_socket(self):
        first = await self.connect()
        await self.request(first, 'prepare')
        await self.service.close()
        self.resume.assert_not_awaited()
        self.assertFalse(self.path.exists())
        self.assertTrue(maintenance.admission_closed(self.app))


if __name__ == '__main__':
    unittest.main()
